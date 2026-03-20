"""Ask Agent Tool -- inter-agent communication via local HTTP.

Sends a message to another specialist agent running on a known local port,
or lists all registered agents with their online/offline status.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

ASK_AGENT_SCHEMA = {
    "name": "ask_agent",
    "description": (
        "Send a message to another specialist agent and get their response, "
        "or list all available agents with their status.\n\n"
        "Use action='list' to see which agents are available and whether they are online.\n"
        "Use action='ask' to send a question/task to a specific agent and get their response."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["ask", "list"],
                "description": "Action to perform. 'list' shows available agents. 'ask' sends a message to an agent.",
            },
            "agent": {
                "type": "string",
                "description": "Target agent name (e.g. 'mirko-researcher'). Required for action='ask'.",
            },
            "message": {
                "type": "string",
                "description": "Message to send to the agent. Required for action='ask'.",
            },
        },
        "required": [],
    },
}

def _default_registry_path() -> Path:
    """Default registry location: HERMES_HOME/agents.yaml (i.e. ~/.hermes/agents.yaml)."""
    hermes_home = os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))
    return Path(hermes_home) / "agents.yaml"


def _load_registry() -> dict:
    """Load agents.yaml registry file."""
    registry_path = os.getenv("HERMES_AGENTS_REGISTRY", str(_default_registry_path()))
    path = Path(registry_path)
    if not path.exists():
        return {}
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("agents", {})
    except Exception as e:
        logger.warning("Failed to load agents registry %s: %s", path, e)
        return {}


def ask_agent_tool(args, **kw):
    """Handle ask_agent tool calls."""
    action = args.get("action", "ask")

    if action == "list":
        return _handle_list()

    return _handle_ask(args)


def _handle_list():
    """List all registered agents with online/offline status."""
    agents = _load_registry()
    if not agents:
        return json.dumps({"error": "No agents registry found. Ensure agents.yaml exists."})

    my_name = os.getenv("HERMES_AGENT_NAME", "")
    my_owner = ""
    if my_name and my_name in agents:
        my_owner = agents[my_name].get("owner", "")

    results = []
    for name, info in agents.items():
        # Only show agents belonging to the same owner
        if my_owner and info.get("owner", "") != my_owner:
            continue

        entry = {
            "name": name,
            "description": info.get("description", ""),
            "port": info.get("port"),
            "is_self": name == my_name,
        }

        # Ping health endpoint
        if name != my_name:
            entry["status"] = _ping_agent(info.get("port"))
        else:
            entry["status"] = "online"

        results.append(entry)

    return json.dumps({"agents": results})


def _ping_agent(port: int) -> str:
    """Check if an agent is online by hitting its health endpoint."""
    if not port:
        return "unknown"
    try:
        import urllib.request
        url = f"http://127.0.0.1:{port}/agent/health"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                return "online"
            return "offline"
    except Exception:
        return "offline"


def _handle_ask(args):
    """Send a message to a specific agent and return the response."""
    agent_name = args.get("agent", "")
    message = args.get("message", "")

    if not agent_name:
        return json.dumps({"error": "Parameter 'agent' is required for action='ask'"})
    if not message:
        return json.dumps({"error": "Parameter 'message' is required for action='ask'"})

    agents = _load_registry()
    if not agents:
        return json.dumps({"error": "No agents registry found."})

    if agent_name not in agents:
        available = [n for n in agents.keys()]
        return json.dumps({
            "error": f"Unknown agent '{agent_name}'",
            "available_agents": available,
        })

    my_name = os.getenv("HERMES_AGENT_NAME", "unknown")
    if agent_name == my_name:
        return json.dumps({"error": "Cannot ask yourself. Use a different agent."})

    agent_info = agents[agent_name]
    port = agent_info.get("port")
    if not port:
        return json.dumps({"error": f"Agent '{agent_name}' has no port configured."})

    # Check same owner
    my_owner = agents.get(my_name, {}).get("owner", "")
    target_owner = agent_info.get("owner", "")
    if my_owner and target_owner and my_owner != target_owner:
        return json.dumps({"error": f"Agent '{agent_name}' belongs to a different owner."})

    try:
        import urllib.request
        url = f"http://127.0.0.1:{port}/agent/ask"
        payload = json.dumps({
            "message": message,
            "from_agent": my_name,
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return json.dumps(result)

    except urllib.error.URLError as e:
        if "Connection refused" in str(e) or "No connection" in str(e):
            return json.dumps({
                "error": f"Agent '{agent_name}' is not running (port {port}). "
                         f"Start it first or check its status with action='list'."
            })
        return json.dumps({"error": f"Failed to reach agent '{agent_name}': {e}"})
    except TimeoutError:
        return json.dumps({
            "error": f"Agent '{agent_name}' timed out after 300s. "
                     "The request may have been too complex."
        })
    except Exception as e:
        if "503" in str(e) or "Service Unavailable" in str(e):
            return json.dumps({
                "error": f"Agent '{agent_name}' is busy processing another request. Try again later."
            })
        return json.dumps({"error": f"Error communicating with agent '{agent_name}': {e}"})


def _check_agent_comm():
    """Gate ask_agent on HERMES_AGENT_NAME being set."""
    return bool(os.getenv("HERMES_AGENT_NAME"))


# --- Registry ---
from tools.registry import registry

registry.register(
    name="ask_agent",
    toolset="agent_comm",
    schema=ASK_AGENT_SCHEMA,
    handler=ask_agent_tool,
    check_fn=_check_agent_comm,
    emoji="🤝",
)
