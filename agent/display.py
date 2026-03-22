"""CLI presentation -- spinner, kawaii faces, tool preview formatting.

Pure display functions and classes with no AIAgent dependency.
Used by AIAgent._execute_tool_calls for CLI feedback.
"""

import json
import logging
import os
import sys
import threading
import time

# ANSI escape codes for coloring tool failure indicators
_RED = "\033[31m"
_RESET = "\033[0m"

logger = logging.getLogger(__name__)


# =========================================================================
# Skin-aware helpers (lazy import to avoid circular deps)
# =========================================================================

def _get_skin():
    """Get the active skin config, or None if not available."""
    try:
        from hermes_cli.skin_engine import get_active_skin
        return get_active_skin()
    except Exception:
        return None


def get_skin_faces(key: str, default: list) -> list:
    """Get spinner face list from active skin, falling back to default."""
    skin = _get_skin()
    if skin:
        faces = skin.get_spinner_list(key)
        if faces:
            return faces
    return default


def get_skin_verbs() -> list:
    """Get thinking verbs from active skin."""
    skin = _get_skin()
    if skin:
        verbs = skin.get_spinner_list("thinking_verbs")
        if verbs:
            return verbs
    return KawaiiSpinner.THINKING_VERBS


def get_skin_tool_prefix() -> str:
    """Get tool output prefix character from active skin."""
    skin = _get_skin()
    if skin:
        return skin.tool_prefix
    return "┊"


def get_tool_emoji(tool_name: str, default: str = "⚡") -> str:
    """Get the display emoji for a tool.

    Resolution order:
    1. Active skin's ``tool_emojis`` overrides (if a skin is loaded)
    2. Tool registry's per-tool ``emoji`` field
    3. *default* fallback
    """
    # 1. Skin override
    skin = _get_skin()
    if skin and skin.tool_emojis:
        override = skin.tool_emojis.get(tool_name)
        if override:
            return override
    # 2. Registry default
    try:
        from tools.registry import registry
        emoji = registry.get_emoji(tool_name, default="")
        if emoji:
            return emoji
    except Exception:
        pass
    # 3. Hardcoded fallback
    return default


# =========================================================================
# Tool preview (one-line summary of a tool call's primary argument)
# =========================================================================

def _oneline(text: str) -> str:
    """Collapse whitespace (including newlines) to single spaces."""
    return " ".join(text.split())


def build_tool_preview(tool_name: str, args: dict, max_len: int = 40) -> str | None:
    """Build a short preview of a tool call's primary argument for display."""
    if not args:
        return None
    primary_args = {
        "terminal": "command", "web_search": "query", "web_extract": "urls",
        "read_file": "path", "write_file": "path", "patch": "path",
        "search_files": "pattern", "browser_navigate": "url",
        "browser_click": "ref", "browser_type": "text",
        "image_generate": "prompt", "text_to_speech": "text",
        "vision_analyze": "question", "mixture_of_agents": "user_prompt",
        "skill_view": "name", "skills_list": "category",
        "cronjob": "action",
        "execute_code": "code", "delegate_task": "goal",
        "clarify": "question", "skill_manage": "name",
    }

    if tool_name == "process":
        action = args.get("action", "")
        sid = args.get("session_id", "")
        data = args.get("data", "")
        timeout_val = args.get("timeout")
        parts = [action]
        if sid:
            parts.append(sid[:16])
        if data:
            parts.append(f'"{_oneline(data[:20])}"')
        if timeout_val and action == "wait":
            parts.append(f"{timeout_val}s")
        return " ".join(parts) if parts else None

    if tool_name == "todo":
        todos_arg = args.get("todos")
        merge = args.get("merge", False)
        if todos_arg is None:
            return "reading task list"
        elif merge:
            return f"updating {len(todos_arg)} task(s)"
        else:
            return f"planning {len(todos_arg)} task(s)"

    if tool_name == "session_search":
        query = _oneline(args.get("query", ""))
        return f"recall: \"{query[:25]}{'...' if len(query) > 25 else ''}\""

    if tool_name == "memory":
        action = args.get("action", "")
        target = args.get("target", "")
        if action == "add":
            content = _oneline(args.get("content", ""))
            return f"+{target}: \"{content[:25]}{'...' if len(content) > 25 else ''}\""
        elif action == "replace":
            return f"~{target}: \"{_oneline(args.get('old_text', '')[:20])}\""
        elif action == "remove":
            return f"-{target}: \"{_oneline(args.get('old_text', '')[:20])}\""
        return action

    if tool_name == "send_message":
        target = args.get("target", "?")
        msg = _oneline(args.get("message", ""))
        if len(msg) > 20:
            msg = msg[:17] + "..."
        return f"to {target}: \"{msg}\""

    if tool_name.startswith("rl_"):
        rl_previews = {
            "rl_list_environments": "listing envs",
            "rl_select_environment": args.get("name", ""),
            "rl_get_current_config": "reading config",
            "rl_edit_config": f"{args.get('field', '')}={args.get('value', '')}",
            "rl_start_training": "starting",
            "rl_check_status": args.get("run_id", "")[:16],
            "rl_stop_training": f"stopping {args.get('run_id', '')[:16]}",
            "rl_get_results": args.get("run_id", "")[:16],
            "rl_list_runs": "listing runs",
            "rl_test_inference": f"{args.get('num_steps', 3)} steps",
        }
        return rl_previews.get(tool_name)

    key = primary_args.get(tool_name)
    if not key:
        for fallback_key in ("query", "text", "command", "path", "name", "prompt", "code", "goal"):
            if fallback_key in args:
                key = fallback_key
                break

    if not key or key not in args:
        return None

    value = args[key]
    if isinstance(value, list):
        value = value[0] if value else ""

    preview = _oneline(str(value))
    if not preview:
        return None
    if len(preview) > max_len:
        preview = preview[:max_len - 3] + "..."
    return preview


def build_tool_status_text(tool_name: str, args: dict | None = None, max_len: int = 60) -> str:
    """Return a short human-readable description of what a tool is doing."""
    args = args or {}

    def _trunc(text: str, limit: int = max_len) -> str:
        text = _oneline(str(text))
        return text if len(text) <= limit else text[: limit - 3] + "..."

    def _domain(url: str) -> str:
        text = str(url or "")
        return text.replace("https://", "").replace("http://", "").split("/")[0] or "the page"

    def _label_path(path: str) -> str:
        text = str(path or "")
        if not text:
            return "a file"
        parts = [part for part in text.replace("\\", "/").split("/") if part]
        return _trunc(parts[-1] if parts else text, 36)

    if tool_name == "web_search":
        query = _trunc(args.get("query", ""), 45)
        return f"Searching the web for \"{query}\"" if query else "Searching the web"
    if tool_name == "web_extract":
        urls = args.get("urls", [])
        url = urls[0] if isinstance(urls, list) and urls else urls
        return f"Reading { _domain(url) }" if url else "Reading the web pages"
    if tool_name == "web_crawl":
        return f"Crawling {_domain(args.get('url', ''))}"
    if tool_name == "terminal":
        return "Running a shell command"
    if tool_name == "process":
        action = args.get("action", "checking")
        labels = {
            "list": "Checking background processes",
            "poll": "Checking background process status",
            "log": "Reading background process output",
            "wait": "Waiting for the background process",
            "kill": "Stopping the background process",
            "write": "Sending input to the background process",
            "submit": "Submitting input to the background process",
        }
        return labels.get(action, f"Handling background process: {action}")
    if tool_name == "read_file":
        return f"Checking {_label_path(args.get('path', ''))}"
    if tool_name == "write_file":
        return f"Writing {_label_path(args.get('path', ''))}"
    if tool_name == "patch":
        return f"Editing {_label_path(args.get('path', ''))}"
    if tool_name == "search_files":
        pattern = _trunc(args.get("pattern", ""), 38)
        if args.get("target") == "files":
            return f"Looking for files matching \"{pattern}\"" if pattern else "Looking for matching files"
        return f"Searching files for \"{pattern}\"" if pattern else "Searching the files"
    if tool_name == "browser_navigate":
        return f"Opening {_domain(args.get('url', ''))}"
    if tool_name == "browser_snapshot":
        return "Inspecting the current page"
    if tool_name == "browser_click":
        ref = args.get("ref", "an element")
        return f"Clicking {ref}"
    if tool_name == "browser_type":
        return "Typing into the page"
    if tool_name == "browser_scroll":
        return f"Scrolling {args.get('direction', 'down')}"
    if tool_name == "browser_back":
        return "Going back"
    if tool_name == "browser_press":
        return f"Pressing {args.get('key', 'a key')}"
    if tool_name == "browser_close":
        return "Closing the browser"
    if tool_name == "browser_get_images":
        return "Collecting images from the page"
    if tool_name == "browser_vision":
        return "Analyzing the page visually"
    if tool_name == "todo":
        todos_arg = args.get("todos")
        if todos_arg is None:
            return "Checking the task list"
        if args.get("merge", False):
            return "Updating the task list"
        return "Planning the work"
    if tool_name == "session_search":
        return "Recalling earlier conversations"
    if tool_name == "memory":
        action = args.get("action", "update")
        if action == "add":
            return "Saving a memory"
        if action == "replace":
            return "Updating a memory"
        if action == "remove":
            return "Removing a memory"
        return "Updating memory"
    if tool_name == "skills_list":
        return "Checking available skills"
    if tool_name == "skill_view":
        return f"Loading the {_trunc(args.get('name', 'requested'), 24)} skill"
    if tool_name == "image_generate":
        return "Generating an image"
    if tool_name == "text_to_speech":
        return "Generating audio"
    if tool_name == "vision_analyze":
        return "Analyzing the image"
    if tool_name == "mixture_of_agents":
        return "Comparing answers across multiple models"
    if tool_name == "send_message":
        return f"Sending a message to {args.get('target', 'the destination')}"
    if tool_name == "cronjob":
        action = args.get("action", "check")
        labels = {
            "create": "Scheduling a task",
            "list": "Checking scheduled tasks",
            "update": "Updating a scheduled task",
            "pause": "Pausing a scheduled task",
            "resume": "Resuming a scheduled task",
            "remove": "Removing a scheduled task",
            "run": "Running a scheduled task now",
        }
        return labels.get(action, f"Handling cron task: {action}")
    if tool_name.startswith("rl_"):
        return "Working with the training environment"
    if tool_name == "execute_code":
        return "Running a short helper script"
    if tool_name == "delegate_task":
        tasks = args.get("tasks")
        if tasks and isinstance(tasks, list):
            return f"Delegating {len(tasks)} task(s) to subagents"
        return "Delegating work to a subagent"
    if tool_name == "clarify":
        return "Asking for clarification"
    if tool_name == "skill_manage":
        return "Updating a skill"

    preview = build_tool_preview(tool_name, args, max_len=40)
    if preview:
        return f"Using {tool_name}: {_trunc(preview, 40)}"
    return f"Using {tool_name}"


def build_tool_progress_topic(
    tool_name: str,
    args: dict | None = None,
    preview: str | None = None,
    max_len: int = 72,
) -> str:
    """Return a higher-level progress update that can collapse many tiny steps."""
    args = args or {}

    def _trunc(text: str, limit: int = max_len) -> str:
        text = _oneline(str(text))
        return text if len(text) <= limit else text[: limit - 3] + "..."

    def _label_path(path: str) -> str:
        text = str(path or "")
        if not text:
            return "the relevant file"
        parts = [part for part in text.replace("\\", "/").split("/") if part]
        return parts[-1] if parts else text

    def _domain(url: str) -> str:
        text = str(url or "")
        return text.replace("https://", "").replace("http://", "").split("/")[0] or "the target site"

    def _focus() -> str | None:
        query = _oneline(str(args.get("query", "")))
        if query:
            return f'"{_trunc(query, 28)}"'

        pattern = _oneline(str(args.get("pattern", "")))
        if pattern:
            return f'"{_trunc(pattern, 28)}"'

        path = args.get("path")
        if path:
            return _trunc(_label_path(str(path)), 28)

        command = _oneline(str(args.get("command", "")))
        if command:
            return f'`{_trunc(command, 28)}`'

        url = args.get("url")
        if url:
            return _trunc(_domain(str(url)), 28)

        urls = args.get("urls")
        if isinstance(urls, list) and urls:
            return _trunc(_domain(str(urls[0])), 28)
        if urls:
            return _trunc(_domain(str(urls)), 28)

        name = _oneline(str(args.get("name", "")))
        if name:
            return f'"{_trunc(name, 28)}"'

        if preview:
            return f'"{_trunc(str(preview), 28)}"'
        return None

    def _with_goal(base: str, reason: str | None = None) -> str:
        if reason:
            return _trunc(f"{base} to {reason}")
        return _trunc(base)

    hint_parts = []
    if preview:
        hint_parts.append(str(preview))
    for key in ("query", "pattern", "path", "url", "command", "text", "name"):
        value = args.get(key)
        if value:
            hint_parts.append(str(value))
    urls = args.get("urls")
    if isinstance(urls, list):
        hint_parts.extend(str(url) for url in urls[:3])
    elif urls:
        hint_parts.append(str(urls))
    hint_text = " ".join(hint_parts).lower()
    focus = _focus()

    config_terms = (
        "config", "configuration", "settings", "setting", "preferences",
        ".yaml", ".yml", ".json", ".toml", ".ini", ".env",
    )
    docs_terms = (
        "docs", "doc", "documentation", "readme", "guide", "reference", "manual", "api",
    )
    model_terms = (
        "model", "models", "provider", "providers", "openrouter", "anthropic", "openai",
        "gpt", "claude", "gemini", "llama", "mistral", "qwen",
    )

    if tool_name == "subagent_progress":
        return _trunc(preview or "Working through delegated tasks")
    if tool_name == "_thinking":
        return _trunc(preview or "Thinking through the task")

    if any(term in hint_text for term in config_terms):
        reason = f"verify the setting in {focus}" if focus else "verify the relevant setting"
        return _with_goal("Checking your config", reason)
    if any(term in hint_text for term in docs_terms) and tool_name in {
        "web_search", "web_extract", "browser_navigate", "browser_snapshot", "search_files", "read_file",
    }:
        reason = f"find {focus}" if focus else "confirm the relevant details"
        return _with_goal("Searching the docs", reason)
    if any(term in hint_text for term in model_terms):
        if args.get("command"):
            reason = "see which models this runtime can use"
        else:
            reason = f"check whether {focus} is available" if focus else "see which models are available"
        return _with_goal("Verifying available models", reason)

    if tool_name in {"read_file", "search_files"}:
        reason = f"find {focus}" if focus else "find the relevant code or data"
        return _with_goal("Inspecting local files", reason)
    if tool_name in {"write_file", "patch"}:
        reason = f"update {focus}" if focus else "apply the requested change"
        return _with_goal("Updating local files", reason)
    if tool_name in {"web_search", "web_extract", "web_crawl"}:
        reason = f"look into {focus}" if focus else "gather the needed information"
        return _with_goal("Researching online", reason)
    if tool_name.startswith("browser_"):
        reason = f"work through {focus}" if focus else "interact with the current page"
        return _with_goal("Working in the browser", reason)
    if tool_name in {"terminal", "process", "execute_code"}:
        reason = f"run {focus}" if focus else "inspect or modify the environment"
        return _with_goal("Running task commands", reason)
    if tool_name == "todo":
        return _with_goal("Planning the work", "track the next concrete steps")
    if tool_name == "delegate_task":
        return _with_goal("Delegating work to subagents", "parallelize the task")

    status_text = build_tool_status_text(tool_name, args, max_len=max_len)
    if focus and focus not in status_text:
        return _with_goal(status_text, f"work on {focus}")
    return _trunc(status_text, max_len)


# =========================================================================
# KawaiiSpinner
# =========================================================================

class KawaiiSpinner:
    """Animated spinner with kawaii faces for CLI feedback during tool execution."""

    SPINNERS = {
        'dots': ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'],
        'bounce': ['⠁', '⠂', '⠄', '⡀', '⢀', '⠠', '⠐', '⠈'],
        'grow': ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█', '▇', '▆', '▅', '▄', '▃', '▂'],
        'arrows': ['←', '↖', '↑', '↗', '→', '↘', '↓', '↙'],
        'star': ['✶', '✷', '✸', '✹', '✺', '✹', '✸', '✷'],
        'moon': ['🌑', '🌒', '🌓', '🌔', '🌕', '🌖', '🌗', '🌘'],
        'pulse': ['◜', '◠', '◝', '◞', '◡', '◟'],
        'brain': ['🧠', '💭', '💡', '✨', '💫', '🌟', '💡', '💭'],
        'sparkle': ['⁺', '˚', '*', '✧', '✦', '✧', '*', '˚'],
    }

    KAWAII_WAITING = [
        "(｡◕‿◕｡)", "(◕‿◕✿)", "٩(◕‿◕｡)۶", "(✿◠‿◠)", "( ˘▽˘)っ",
        "♪(´ε` )", "(◕ᴗ◕✿)", "ヾ(＾∇＾)", "(≧◡≦)", "(★ω★)",
    ]

    KAWAII_THINKING = [
        "(｡•́︿•̀｡)", "(◔_◔)", "(¬‿¬)", "( •_•)>⌐■-■", "(⌐■_■)",
        "(´･_･`)", "◉_◉", "(°ロ°)", "( ˘⌣˘)♡", "ヽ(>∀<☆)☆",
        "٩(๑❛ᴗ❛๑)۶", "(⊙_⊙)", "(¬_¬)", "( ͡° ͜ʖ ͡°)", "ಠ_ಠ",
    ]

    THINKING_VERBS = [
        "pondering", "contemplating", "musing", "cogitating", "ruminating",
        "deliberating", "mulling", "reflecting", "processing", "reasoning",
        "analyzing", "computing", "synthesizing", "formulating", "brainstorming",
    ]

    def __init__(self, message: str = "", spinner_type: str = 'dots'):
        self.message = message
        self.spinner_frames = self.SPINNERS.get(spinner_type, self.SPINNERS['dots'])
        self.running = False
        self.thread = None
        self.frame_idx = 0
        self.start_time = None
        self.last_line_len = 0
        self._last_flush_time = 0.0  # Rate-limit flushes for patch_stdout compat
        # Capture stdout NOW, before any redirect_stdout(devnull) from
        # child agents can replace sys.stdout with a black hole.
        self._out = sys.stdout
        # Disable spinner when stdout is not a terminal (e.g. launchd
        # redirecting to a log file) — animation frames are meaningless
        # in log files and cause massive bloat.
        self._disabled = not hasattr(self._out, 'isatty') or not self._out.isatty()

    def _write(self, text: str, end: str = '\n', flush: bool = False):
        """Write to the stdout captured at spinner creation time."""
        if self._disabled:
            return
        try:
            self._out.write(text + end)
            if flush:
                self._out.flush()
        except (ValueError, OSError):
            pass

    def _animate(self):
        # Cache skin wings at start (avoid per-frame imports)
        skin = _get_skin()
        wings = skin.get_spinner_wings() if skin else []

        while self.running:
            if os.getenv("HERMES_SPINNER_PAUSE"):
                time.sleep(0.1)
                continue
            frame = self.spinner_frames[self.frame_idx % len(self.spinner_frames)]
            elapsed = time.time() - self.start_time
            if wings:
                left, right = wings[self.frame_idx % len(wings)]
                line = f"  {left} {frame} {self.message} {right} ({elapsed:.1f}s)"
            else:
                line = f"  {frame} {self.message} ({elapsed:.1f}s)"
            pad = max(self.last_line_len - len(line), 0)
            # Rate-limit flush() calls to avoid spinner spam under
            # prompt_toolkit's patch_stdout.  Each flush() pushes a queue
            # item that may trigger a separate run_in_terminal() call; if
            # items are processed one-at-a-time the \r overwrite is lost
            # and every frame appears on its own line.  By flushing at
            # most every 0.4s we guarantee multiple \r-frames are batched
            # into a single write, so the terminal collapses them correctly.
            now = time.time()
            should_flush = (now - self._last_flush_time) >= 0.4
            self._write(f"\r{line}{' ' * pad}", end='', flush=should_flush)
            if should_flush:
                self._last_flush_time = now
            self.last_line_len = len(line)
            self.frame_idx += 1
            time.sleep(0.12)

    def start(self):
        if self.running or self._disabled:
            return
        self.running = True
        self.start_time = time.time()
        self.thread = threading.Thread(target=self._animate, daemon=True)
        self.thread.start()

    def update_text(self, new_message: str):
        self.message = new_message

    def print_above(self, text: str):
        """Print a line above the spinner without disrupting animation.

        Clears the current spinner line, prints the text, and lets the
        next animation tick redraw the spinner on the line below.
        Thread-safe: uses the captured stdout reference (self._out).
        Works inside redirect_stdout(devnull) because _write bypasses
        sys.stdout and writes to the stdout captured at spinner creation.
        """
        if not self.running:
            self._write(f"  {text}", flush=True)
            return
        # Clear spinner line with spaces (not \033[K) to avoid garbled escape
        # codes when prompt_toolkit's patch_stdout is active — same approach
        # as stop(). Then print text; spinner redraws on next tick.
        blanks = ' ' * max(self.last_line_len + 5, 40)
        self._write(f"\r{blanks}\r  {text}", flush=True)

    def stop(self, final_message: str = None):
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)
        # Clear the spinner line with spaces instead of \033[K to avoid
        # garbled escape codes when prompt_toolkit's patch_stdout is active.
        blanks = ' ' * max(self.last_line_len + 5, 40)
        self._write(f"\r{blanks}\r", end='', flush=True)
        if final_message:
            self._write(f"  {final_message}", flush=True)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False


# =========================================================================
# Kawaii face arrays (used by AIAgent._execute_tool_calls for spinner text)
# =========================================================================

KAWAII_SEARCH = [
    "♪(´ε` )", "(｡◕‿◕｡)", "ヾ(＾∇＾)", "(◕ᴗ◕✿)", "( ˘▽˘)っ",
    "٩(◕‿◕｡)۶", "(✿◠‿◠)", "♪～(´ε｀ )", "(ノ´ヮ`)ノ*:・゚✧", "＼(◎o◎)／",
]
KAWAII_READ = [
    "φ(゜▽゜*)♪", "( ˘▽˘)っ", "(⌐■_■)", "٩(｡•́‿•̀｡)۶", "(◕‿◕✿)",
    "ヾ(＠⌒ー⌒＠)ノ", "(✧ω✧)", "♪(๑ᴖ◡ᴖ๑)♪", "(≧◡≦)", "( ´ ▽ ` )ノ",
]
KAWAII_TERMINAL = [
    "ヽ(>∀<☆)ノ", "(ノ°∀°)ノ", "٩(^ᴗ^)۶", "ヾ(⌐■_■)ノ♪", "(•̀ᴗ•́)و",
    "┗(＾0＾)┓", "(｀・ω・´)", "＼(￣▽￣)／", "(ง •̀_•́)ง", "ヽ(´▽`)/",
]
KAWAII_BROWSER = [
    "(ノ°∀°)ノ", "(☞゚ヮ゚)☞", "( ͡° ͜ʖ ͡°)", "┌( ಠ_ಠ)┘", "(⊙_⊙)？",
    "ヾ(•ω•`)o", "(￣ω￣)", "( ˇωˇ )", "(ᵔᴥᵔ)", "＼(◎o◎)／",
]
KAWAII_CREATE = [
    "✧*。٩(ˊᗜˋ*)و✧", "(ﾉ◕ヮ◕)ﾉ*:・ﾟ✧", "ヽ(>∀<☆)ノ", "٩(♡ε♡)۶", "(◕‿◕)♡",
    "✿◕ ‿ ◕✿", "(*≧▽≦)", "ヾ(＾-＾)ノ", "(☆▽☆)", "°˖✧◝(⁰▿⁰)◜✧˖°",
]
KAWAII_SKILL = [
    "ヾ(＠⌒ー⌒＠)ノ", "(๑˃ᴗ˂)ﻭ", "٩(◕‿◕｡)۶", "(✿╹◡╹)", "ヽ(・∀・)ノ",
    "(ノ´ヮ`)ノ*:・ﾟ✧", "♪(๑ᴖ◡ᴖ๑)♪", "(◠‿◠)", "٩(ˊᗜˋ*)و", "(＾▽＾)",
    "ヾ(＾∇＾)", "(★ω★)/", "٩(｡•́‿•̀｡)۶", "(◕ᴗ◕✿)", "＼(◎o◎)／",
    "(✧ω✧)", "ヽ(>∀<☆)ノ", "( ˘▽˘)っ", "(≧◡≦) ♡", "ヾ(￣▽￣)",
]
KAWAII_THINK = [
    "(っ°Д°;)っ", "(；′⌒`)", "(・_・ヾ", "( ´_ゝ`)", "(￣ヘ￣)",
    "(。-`ω´-)", "( ˘︹˘ )", "(¬_¬)", "ヽ(ー_ー )ノ", "(；一_一)",
]
KAWAII_GENERIC = [
    "♪(´ε` )", "(◕‿◕✿)", "ヾ(＾∇＾)", "٩(◕‿◕｡)۶", "(✿◠‿◠)",
    "(ノ´ヮ`)ノ*:・ﾟ✧", "ヽ(>∀<☆)ノ", "(☆▽☆)", "( ˘▽˘)っ", "(≧◡≦)",
]


# =========================================================================
# Cute tool message (completion line that replaces the spinner)
# =========================================================================

def _detect_tool_failure(tool_name: str, result: str | None) -> tuple[bool, str]:
    """Inspect a tool result string for signs of failure.

    Returns ``(is_failure, suffix)`` where *suffix* is an informational tag
    like ``" [exit 1]"`` for terminal failures, or ``" [error]"`` for generic
    failures.  On success, returns ``(False, "")``.
    """
    if result is None:
        return False, ""

    if tool_name == "terminal":
        try:
            data = json.loads(result)
            exit_code = data.get("exit_code")
            if exit_code is not None and exit_code != 0:
                return True, f" [exit {exit_code}]"
        except (json.JSONDecodeError, TypeError, AttributeError):
            logger.debug("Could not parse terminal result as JSON for exit code check")
        return False, ""

    # Memory-specific: distinguish "full" from real errors
    if tool_name == "memory":
        try:
            data = json.loads(result)
            if data.get("success") is False and "exceed the limit" in data.get("error", ""):
                return True, " [full]"
        except (json.JSONDecodeError, TypeError, AttributeError):
            logger.debug("Could not parse memory result as JSON for capacity check")

    # Generic heuristic for non-terminal tools
    lower = result[:500].lower()
    if '"error"' in lower or '"failed"' in lower or result.startswith("Error"):
        return True, " [error]"

    return False, ""


def get_cute_tool_message(
    tool_name: str, args: dict, duration: float, result: str | None = None,
) -> str:
    """Generate a human-readable tool completion line for CLI quiet mode."""
    dur = f"{duration:.1f}s"
    is_failure, failure_suffix = _detect_tool_failure(tool_name, result)
    skin_prefix = get_skin_tool_prefix()
    emoji = get_tool_emoji(tool_name, default="⚡")
    status_text = build_tool_status_text(tool_name, args, max_len=56)

    line = f"┊ {emoji} {status_text}  {dur}"
    if skin_prefix != "┊":
        line = line.replace("┊", skin_prefix, 1)
    if is_failure:
        line = f"{line}{failure_suffix}"
    return line


# =========================================================================
# Honcho session line (one-liner with clickable OSC 8 hyperlink)
# =========================================================================

_DIM = "\033[2m"
_SKY_BLUE = "\033[38;5;117m"
_ANSI_RESET = "\033[0m"


def honcho_session_url(workspace: str, session_name: str) -> str:
    """Build a Honcho app URL for a session."""
    from urllib.parse import quote
    return (
        f"https://app.honcho.dev/explore"
        f"?workspace={quote(workspace, safe='')}"
        f"&view=sessions"
        f"&session={quote(session_name, safe='')}"
    )


def _osc8_link(url: str, text: str) -> str:
    """OSC 8 terminal hyperlink (clickable in iTerm2, Ghostty, WezTerm, etc.)."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def honcho_session_line(workspace: str, session_name: str) -> str:
    """One-line session indicator: `Honcho session: <clickable name>`."""
    url = honcho_session_url(workspace, session_name)
    linked_name = _osc8_link(url, f"{_SKY_BLUE}{session_name}{_ANSI_RESET}")
    return f"{_DIM}Honcho session:{_ANSI_RESET} {linked_name}"


def write_tty(text: str) -> None:
    """Write directly to /dev/tty, bypassing stdout capture."""
    try:
        fd = os.open("/dev/tty", os.O_WRONLY)
        os.write(fd, text.encode("utf-8"))
        os.close(fd)
    except OSError:
        sys.stdout.write(text)
        sys.stdout.flush()
