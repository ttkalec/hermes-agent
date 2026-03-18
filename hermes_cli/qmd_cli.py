from __future__ import annotations

from datetime import datetime

from agent.qmd_memory import QmdMemoryManager
from hermes_cli.config import get_hermes_home, load_config


def _fmt_ts(value):
    if not value:
        return "never"
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


def qmd_command(args) -> int:
    cfg = load_config().get("memory", {})
    backend = str(cfg.get("backend", "local") or "local")
    mgr = QmdMemoryManager(get_hermes_home(), cfg)

    if getattr(args, "qmd_command", None) == "sync":
        changed = mgr.sync(force=True)
        print(f"QMD sync complete (changed={changed})")
        if mgr.last_error:
            print(f"Last error: {mgr.last_error}")
            return 1
        return 0

    info = mgr.status_info(sync=getattr(args, "sync", False))
    print("QMD status")
    print(f"  configured backend: {backend}")
    print(f"  active command: {info['command']}")
    print(f"  search mode: {info['search_mode']}")
    print(f"  memory path: {info['memory_path']} ({info['memory_files']} files)")
    print(f"  sessions enabled: {info['sessions_enabled']}")
    print(f"  sessions export dir: {info['sessions_export_dir']} ({info['session_files']} files)")
    print(f"  last sync: {_fmt_ts(info['last_sync'])}")
    if info.get("last_session_export"):
        exp = info["last_session_export"]
        print(f"  last session export: exported={exp.get('exported', 0)} removed={exp.get('removed', 0)} kept={exp.get('kept', 0)}")
    print(f"  last error: {info['last_error'] or 'none'}")
    if info.get("collections_text"):
        print()
        print(info["collections_text"])
    return 0
