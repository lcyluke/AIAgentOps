"""Apex Fleet Manager — multi-Mac Hermes fleet orchestration.

Design principles:
  1. ONE Origin per fleet. Origin holds cron, dashboard, authorization.
  2. Worker Macs pull skills/profiles from Origin's Git repo.
  3. Each Mac has independent state.db (local memory).
  4. Origin transfer requires dual-approval (current Origin + new Origin).
  5. GitHub repo (hermes-fleet-config) is the sync backbone.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


APEX_HOME = Path(os.path.expanduser("~/.apex"))
HERMES_HOME = Path(os.path.expanduser("~/.hermes"))
FLEET_CONFIG = APEX_HOME / "fleet.json"
FLEET_REPO = "hermes-fleet-config"  # GitHub repo name for config sync


# ── Data Models ──────────────────────────────────────────────

@dataclass
class FleetNode:
    """A single Mac in the fleet."""
    node_id: str
    hostname: str
    role: str  # "origin" | "worker"
    projects: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    last_seen: str = ""
    ip: str = ""
    os_version: str = ""


@dataclass
class FleetConfig:
    """Fleet-wide configuration."""
    fleet_id: str
    fleet_name: str
    origin_node_id: str
    git_repo: str  # GitHub URL for config sync
    nodes: dict[str, FleetNode] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


# ── Fleet Manager ────────────────────────────────────────────

class FleetManager:
    """Manages multi-Mac Hermes fleet."""

    def __init__(self):
        self._config: Optional[FleetConfig] = None
        self._load()

    # ── Config I/O ───────────────────────────────────────────

    def _load(self):
        if FLEET_CONFIG.exists():
            data = json.loads(FLEET_CONFIG.read_text())
            nodes = {
                k: FleetNode(**v) for k, v in data.get("nodes", {}).items()
            }
            self._config = FleetConfig(
                fleet_id=data.get("fleet_id", ""),
                fleet_name=data.get("fleet_name", "Unnamed Fleet"),
                origin_node_id=data.get("origin_node_id", ""),
                git_repo=data.get("git_repo", ""),
                nodes=nodes,
                created_at=data.get("created_at", ""),
                updated_at=data.get("updated_at", ""),
            )
        else:
            self._config = None

    def _save(self):
        if self._config:
            data = {
                "fleet_id": self._config.fleet_id,
                "fleet_name": self._config.fleet_name,
                "origin_node_id": self._config.origin_node_id,
                "git_repo": self._config.git_repo,
                "nodes": {
                    k: {
                        "node_id": v.node_id,
                        "hostname": v.hostname,
                        "role": v.role,
                        "projects": v.projects,
                        "agents": v.agents,
                        "last_seen": v.last_seen,
                    }
                    for k, v in self._config.nodes.items()
                },
                "created_at": self._config.created_at,
                "updated_at": datetime.now().isoformat(),
            }
            FLEET_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            FLEET_CONFIG.write_text(json.dumps(data, indent=2))

    # ── Fleet Init ───────────────────────────────────────────

    def init_fleet(self, name: str, git_repo: str) -> dict:
        """Initialize this Mac as the fleet Origin."""
        import platform
        hostname = platform.node()
        node_id = f"node-{uuid.uuid4().hex[:8]}"

        now = datetime.now().isoformat()
        self._config = FleetConfig(
            fleet_id=f"fleet-{uuid.uuid4().hex[:8]}",
            fleet_name=name,
            origin_node_id=node_id,
            git_repo=git_repo,
            nodes={
                node_id: FleetNode(
                    node_id=node_id,
                    hostname=hostname,
                    role="origin",
                    last_seen=now,
                    os_version=platform.platform(),
                )
            },
            created_at=now,
            updated_at=now,
        )
        self._save()

        # Init git repo in ~/.hermes for config sync
        self._init_git_repo(git_repo)

        return {"ok": True, "node_id": node_id, "role": "origin"}

    def join_fleet(self, git_repo: str) -> dict:
        """Join an existing fleet as a worker node."""
        import platform
        hostname = platform.node()
        node_id = f"node-{uuid.uuid4().hex[:8]}"

        # Clone config from git
        if not self._clone_config(git_repo):
            return {"ok": False, "error": "Failed to clone fleet config repo"}

        # Register this node
        self._load()
        if not self._config:
            return {"ok": False, "error": "Invalid fleet config"}

        self._config.nodes[node_id] = FleetNode(
            node_id=node_id,
            hostname=hostname,
            role="worker",
            last_seen=datetime.now().isoformat(),
            os_version=platform.platform(),
        )
        self._save()
        self._push_config()

        return {"ok": True, "node_id": node_id, "role": "worker"}

    # ── Origin Management ────────────────────────────────────

    def is_origin(self) -> bool:
        """Check if this Mac is the fleet Origin."""
        if not self._config:
            return False
        import platform
        hostname = platform.node()
        for node in self._config.nodes.values():
            if node.hostname == hostname and node.role == "origin":
                return True
        return False

    def request_origin(self, reason: str = "") -> dict:
        """Request to become the Origin. Returns a request code for approval."""
        if not self._config:
            return {"ok": False, "error": "No fleet configured"}

        request_code = f"origin-req-{uuid.uuid4().hex[:8]}"
        # Store pending request
        pending = APEX_HOME / "origin_requests.json"
        requests = json.loads(pending.read_text()) if pending.exists() else {}
        requests[request_code] = {
            "from_node": self._get_my_node_id(),
            "reason": reason,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
        }
        pending.write_text(json.dumps(requests, indent=2))

        return {
            "ok": True,
            "request_code": request_code,
            "message": f"Origin transfer requested. Current Origin must run: apex origin approve {request_code}",
        }

    def approve_origin(self, request_code: str) -> dict:
        """Current Origin approves a transfer request."""
        if not self.is_origin():
            return {"ok": False, "error": "Only the current Origin can approve transfers"}

        pending = APEX_HOME / "origin_requests.json"
        if not pending.exists():
            return {"ok": False, "error": "No pending requests"}

        requests = json.loads(pending.read_text())
        if request_code not in requests:
            return {"ok": False, "error": "Request not found"}

        req = requests[request_code]
        new_origin_id = req["from_node"]

        # Transfer Origin role
        old_origin = None
        for node in self._config.nodes.values():
            if node.role == "origin":
                old_origin = node.node_id
                node.role = "worker"
            if node.node_id == new_origin_id:
                node.role = "origin"

        self._config.origin_node_id = new_origin_id
        self._save()
        self._push_config()

        req["status"] = "approved"
        pending.write_text(json.dumps(requests, indent=2))

        return {
            "ok": True,
            "old_origin": old_origin,
            "new_origin": new_origin_id,
            "message": "Origin transferred. New Origin must run: apex fleet sync",
        }

    # ── Fleet Status ─────────────────────────────────────────

    def fleet_status(self) -> dict:
        """Return fleet-wide status."""
        if not self._config:
            return {"ok": False, "error": "No fleet configured"}

        nodes_list = []
        for node in self._config.nodes.values():
            nodes_list.append({
                "node_id": node.node_id,
                "hostname": node.hostname,
                "role": node.role,
                "projects": len(node.projects),
                "agents": len(node.agents),
                "last_seen": node.last_seen[:16] if node.last_seen else "-",
            })

        return {
            "ok": True,
            "fleet_name": self._config.fleet_name,
            "origin": self._config.origin_node_id,
            "total_nodes": len(nodes_list),
            "git_repo": self._config.git_repo,
            "nodes": nodes_list,
            "is_origin": self.is_origin(),
        }

    # ── Sync ──────────────────────────────────────────────────

    def sync_config(self) -> dict:
        """Pull latest fleet config from git, push local changes."""
        if not self._config:
            return {"ok": False, "error": "No fleet configured"}

        try:
            hermes_dir = str(HERMES_HOME)
            subprocess.run(["git", "-C", hermes_dir, "pull"], capture_output=True, timeout=30)
            subprocess.run(["git", "-C", hermes_dir, "add", "-A"], capture_output=True)
            subprocess.run(
                ["git", "-C", hermes_dir, "commit", "-m", "Fleet sync"],
                capture_output=True, timeout=10,
            )
            subprocess.run(["git", "-C", hermes_dir, "push"], capture_output=True, timeout=30)
            return {"ok": True, "message": "Config synced"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Project Management ───────────────────────────────────

    def register_project(self, project_name: str, pm_agent: str) -> dict:
        """Register a project on this node."""
        node_id = self._get_my_node_id()
        if not node_id or not self._config:
            return {"ok": False, "error": "Not in a fleet"}

        node = self._config.nodes.get(node_id)
        if not node:
            return {"ok": False, "error": "Node not found"}

        if project_name not in node.projects:
            node.projects.append(project_name)
        if pm_agent not in node.agents:
            node.agents.append(pm_agent)

        self._save()
        return {"ok": True, "project": project_name, "pm": pm_agent}

    # ── Helpers ──────────────────────────────────────────────

    def _get_my_node_id(self) -> Optional[str]:
        import platform
        hostname = platform.node()
        if self._config:
            for node in self._config.nodes.values():
                if node.hostname == hostname:
                    return node.node_id
        return None

    def _init_git_repo(self, git_repo: str):
        """Initialize ~/.hermes as a git repo for fleet config sync."""
        try:
            hermes_dir = str(HERMES_HOME)
            # Init if not already
            if not (HERMES_HOME / ".git").exists():
                subprocess.run(["git", "-C", hermes_dir, "init"], capture_output=True)
                gitignore = HERMES_HOME / ".gitignore"
                gitignore.write_text("state.db\naudio_cache/\ncheckpoints/\ncache/\n*.lock\n*.pid\n")
                subprocess.run(["git", "-C", hermes_dir, "add", "-A"], capture_output=True)
                subprocess.run(
                    ["git", "-C", hermes_dir, "commit", "-m", "Fleet origin init"],
                    capture_output=True,
                )
            subprocess.run(
                ["git", "-C", hermes_dir, "remote", "add", "origin", git_repo],
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", hermes_dir, "remote", "set-url", "origin", git_repo],
                capture_output=True,
            )
        except Exception:
            pass

    def _clone_config(self, git_repo: str) -> bool:
        """Clone fleet config from git into ~/.hermes."""
        try:
            # Backup existing
            backup = HERMES_HOME.parent / ".hermes.bak.fleet"
            if HERMES_HOME.exists():
                subprocess.run(["mv", str(HERMES_HOME), str(backup)], capture_output=True)

            r = subprocess.run(
                ["git", "clone", git_repo, str(HERMES_HOME)],
                capture_output=True, timeout=60,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _push_config(self):
        """Push fleet config changes to git."""
        try:
            hermes_dir = str(HERMES_HOME)
            subprocess.run(["git", "-C", hermes_dir, "add", "fleet.json"], capture_output=True)
            subprocess.run(
                ["git", "-C", hermes_dir, "commit", "-m", "Fleet update"],
                capture_output=True, timeout=10,
            )
            subprocess.run(["git", "-C", hermes_dir, "push"], capture_output=True, timeout=30)
        except Exception:
            pass
