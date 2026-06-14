"""
MCP roots handler — V5.1 Phase 15' hardening.

Tracks per-client root state across disconnect-reconnect cycles.
When a client emits notifications/roots/list_changed the server-side
fetch is already done by the caller (MCP SDK); the handler receives
the resulting roots list.

Hard Rule 54: no psutil.process_iter(). Discovery is MCP roots only.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

RegisterFn = Callable[[str], Awaitable[None]]
EmitFn = Callable[[str, dict], None]


@dataclass
class ClientRootState:
    """Per-client root state. Retained across disconnect-reconnect cycles."""
    client_id: str
    roots: dict[str, str] = field(default_factory=dict)  # uri → name
    connected_at: datetime | None = None
    last_seen_at: datetime | None = None
    disconnected_at: datetime | None = None

    @property
    def is_connected(self) -> bool:
        if self.connected_at is None:
            return False
        if self.disconnected_at is None:
            return True
        return self.connected_at > self.disconnected_at


class McpRootsHandler:
    """
    Manages MCP client root state and triggers project registration.

    Hardened for disconnect-reconnect: ClientRootState is never evicted
    from memory — only updated. On reconnect the previous root map is
    reused and incrementally diffed against the new list.

    Wire-up (example with the official `mcp` SDK):

        handler = McpRootsHandler(register_project=core.register_project)

        @server.notification("notifications/roots/list_changed")
        async def on_roots_changed(ctx):
            roots = await ctx.session.list_roots()
            await handler.handle_roots_list_changed(
                client_id=ctx.client_id,
                new_roots=[r.model_dump() for r in roots.roots],
            )
    """

    def __init__(
        self,
        register_project: RegisterFn,
        emit_event: EmitFn | None = None,
    ) -> None:
        self._clients: dict[str, ClientRootState] = {}
        self._register_project = register_project
        self._emit_event: EmitFn = emit_event or (lambda _event, _data: None)

    # ── Connection lifecycle ────────────────────────────────────────────────

    def handle_client_connect(self, client_id: str) -> None:
        """Called when a new MCP session is established."""
        now = datetime.now(timezone.utc)
        if client_id in self._clients:
            state = self._clients[client_id]
            state.connected_at = now
            state.last_seen_at = now
            state.disconnected_at = None
            log.info("mcp_roots: client reconnected client_id=%s", client_id)
        else:
            self._clients[client_id] = ClientRootState(
                client_id=client_id,
                connected_at=now,
                last_seen_at=now,
            )
            log.info("mcp_roots: client connected client_id=%s", client_id)

    def handle_client_disconnect(self, client_id: str) -> None:
        """
        Called when a client session ends.

        Projects are retained in the DB; only the connectivity state is updated
        so the dashboard can show the client as offline.
        """
        now = datetime.now(timezone.utc)
        if client_id in self._clients:
            self._clients[client_id].disconnected_at = now
            log.info("mcp_roots: client disconnected client_id=%s", client_id)
            self._emit_event(
                "client_gone",
                {"client_id": client_id, "ts": now.isoformat()},
            )
        else:
            log.debug("mcp_roots: disconnect for unknown client client_id=%s", client_id)

    # ── Root change handling ────────────────────────────────────────────────

    async def handle_roots_list_changed(
        self,
        client_id: str,
        new_roots: list[dict[str, Any]],
    ) -> None:
        """
        Process a roots/list_changed notification.

        new_roots: list of {"uri": "file:///...", "name": "..."} dicts
        as returned by the MCP roots/list response.

        New URIs trigger register_project. Removed URIs are logged but
        NOT unregistered (user may still want project history).
        """
        now = datetime.now(timezone.utc)

        if client_id not in self._clients:
            self.handle_client_connect(client_id)

        state = self._clients[client_id]
        state.last_seen_at = now

        new_map: dict[str, str] = {
            r["uri"]: r.get("name", r["uri"]) for r in new_roots
        }
        previous_map = state.roots

        added = {uri: name for uri, name in new_map.items() if uri not in previous_map}
        removed = {uri: name for uri, name in previous_map.items() if uri not in new_map}

        state.roots = new_map

        for uri, name in added.items():
            path = _uri_to_local_path(uri)
            if path is None:
                log.debug("mcp_roots: skipping non-file URI uri=%s", uri)
                continue
            log.info(
                "mcp_roots: registering new root client_id=%s uri=%s path=%s",
                client_id, uri, path,
            )
            try:
                await self._register_project(path)
            except Exception as exc:
                log.warning(
                    "mcp_roots: register_project failed path=%s err=%s", path, exc
                )

        if removed:
            log.info(
                "mcp_roots: removed roots retained in DB client_id=%s count=%d uris=%s",
                client_id,
                len(removed),
                list(removed.keys()),
            )

    # ── Accessors ───────────────────────────────────────────────────────────

    def get_client_state(self, client_id: str) -> ClientRootState | None:
        return self._clients.get(client_id)

    def all_clients(self) -> dict[str, ClientRootState]:
        return dict(self._clients)

    def connected_client_ids(self) -> list[str]:
        return [c for c, s in self._clients.items() if s.is_connected]


# ── Helpers ─────────────────────────────────────────────────────────────────

def _uri_to_local_path(uri: str) -> str | None:
    """
    Convert a file:// URI to an absolute local filesystem path.

    Handles both POSIX (file:///home/user/proj) and Windows
    (file:///C:/Users/user/proj → C:/Users/user/proj) forms.
    Returns None for non-file URIs.
    """
    if not uri.startswith("file://"):
        return None
    path = uri[len("file://"):]
    # file:///C:/... → /C:/... → C:/...
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path or None
