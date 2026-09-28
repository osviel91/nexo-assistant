from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any


class ProfileValidationError(ValueError):
    pass


class ProfileNotFoundError(LookupError):
    pass


class ProfileResolutionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AgentProfile:
    id: str
    name: str
    description: str
    provider_id: str
    model_id: str
    system_instructions: str
    model_parameters: dict[str, Any]
    enabled: bool
    created_at: str
    updated_at: str
    tool_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentProfileInput:
    name: str
    description: str = ""
    provider_id: str = ""
    model_id: str = ""
    system_instructions: str = ""
    model_parameters: dict[str, Any] | None = None
    enabled: bool = True
    tool_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentRunConfiguration:
    profile_id: str
    provider_id: str
    model_id: str
    system_instructions: str
    temperature: float | None
    requested_tool_names: tuple[str, ...]


class AgentProfileRepository:
    def __init__(self, connection_factory: Callable[[], sqlite3.Connection], now: Callable[[], str]) -> None:
        self.connection_factory = connection_factory
        self.now = now

    def _tools(self, connection: sqlite3.Connection, profile_id: str) -> tuple[str, ...]:
        return tuple(row[0] for row in connection.execute(
            "SELECT tool_name FROM agent_profile_tools WHERE agent_profile_id=? ORDER BY tool_name", (profile_id,)
        ))

    def _row(self, connection: sqlite3.Connection, profile_id: str) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM agent_profiles WHERE id=?", (profile_id,)).fetchone()

    def get(self, profile_id: str) -> tuple[sqlite3.Row, tuple[str, ...]] | None:
        with self.connection_factory() as connection:
            row = self._row(connection, profile_id)
            return None if row is None else (row, self._tools(connection, profile_id))

    def list(self) -> list[tuple[sqlite3.Row, tuple[str, ...]]]:
        with self.connection_factory() as connection:
            rows = connection.execute("SELECT * FROM agent_profiles ORDER BY name, id").fetchall()
            return [(row, self._tools(connection, row["id"])) for row in rows]

    def create(self, item: AgentProfileInput) -> tuple[sqlite3.Row, tuple[str, ...]]:
        profile_id, timestamp = str(uuid.uuid4()), self.now()
        with self.connection_factory() as connection:
            connection.execute(
                """INSERT INTO agent_profiles
                (id,name,description,provider_id,model_id,system_instructions,model_parameters,enabled,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (profile_id, item.name, item.description, item.provider_id, item.model_id,
                 item.system_instructions, json.dumps(item.model_parameters or {}), int(item.enabled), timestamp, timestamp),
            )
            connection.executemany("INSERT INTO agent_profile_tools(agent_profile_id,tool_name) VALUES(?,?)",
                                   [(profile_id, name) for name in item.tool_names])
            return self._row(connection, profile_id), item.tool_names

    def update(self, profile_id: str, values: dict[str, Any]) -> tuple[sqlite3.Row, tuple[str, ...]]:
        with self.connection_factory() as connection:
            if self._row(connection, profile_id) is None:
                raise ProfileNotFoundError(profile_id)
            if values:
                columns = [key for key in values if key != "tool_names"]
                if columns:
                    assignments = ", ".join(f"{column}=?" for column in columns)
                    params = [json.dumps(values[column]) if column == "model_parameters" else values[column] for column in columns]
                    connection.execute(f"UPDATE agent_profiles SET {assignments}, updated_at=? WHERE id=?", (*params, self.now(), profile_id))
                if "tool_names" in values:
                    connection.execute("DELETE FROM agent_profile_tools WHERE agent_profile_id=?", (profile_id,))
                    connection.executemany("INSERT INTO agent_profile_tools(agent_profile_id,tool_name) VALUES(?,?)",
                                           [(profile_id, name) for name in values["tool_names"]])
            row = self._row(connection, profile_id)
            return row, self._tools(connection, profile_id)

    def delete(self, profile_id: str) -> bool:
        with self.connection_factory() as connection:
            cursor = connection.execute("DELETE FROM agent_profiles WHERE id=?", (profile_id,))
            return cursor.rowcount > 0


class AgentProfileService:
    def __init__(self, repository: AgentProfileRepository, tool_catalog: Callable[[], Iterable[dict[str, Any]]]) -> None:
        self.repository = repository
        self.tool_catalog = tool_catalog

    @staticmethod
    def validate(item: AgentProfileInput, require_references: bool = True) -> AgentProfileInput:
        name = item.name.strip()
        if not name or len(name) > 120:
            raise ProfileValidationError("name must be 1-120 characters")
        if len(item.description) > 2000 or len(item.system_instructions) > 20000:
            raise ProfileValidationError("description or system_instructions is too long")
        if require_references and (not item.provider_id.strip() or not item.model_id.strip()):
            raise ProfileValidationError("provider_id and model_id are required")
        parameters = item.model_parameters or {}
        if not isinstance(parameters, dict) or set(parameters) - {"temperature"}:
            raise ProfileValidationError("only temperature is supported in model_parameters")
        if "temperature" in parameters and (isinstance(parameters["temperature"], bool) or not isinstance(parameters["temperature"], (int, float)) or not 0 <= parameters["temperature"] <= 2):
            raise ProfileValidationError("temperature must be a number between 0 and 2")
        tools = tuple(sorted(set(item.tool_names)))
        if any(not isinstance(tool, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:__[A-Za-z0-9_-]+)*", tool) or len(tool) > 200 for tool in tools):
            raise ProfileValidationError("tool names are invalid")
        return AgentProfileInput(name, item.description, item.provider_id.strip(), item.model_id.strip(),
                                 item.system_instructions, parameters, item.enabled, tools)

    def _validate_references(self, item: AgentProfileInput) -> None:
        with self.repository.connection_factory() as connection:
            if connection.execute("SELECT 1 FROM providers WHERE id=?", (item.provider_id,)).fetchone() is None:
                raise ProfileValidationError("provider_id is invalid")
            if connection.execute("SELECT 1 FROM models WHERE provider_id=? AND id=?", (item.provider_id, item.model_id)).fetchone() is None:
                raise ProfileValidationError("model_id is invalid for provider_id")

    def _output(self, stored: tuple[sqlite3.Row, tuple[str, ...]]) -> dict[str, Any]:
        row, tool_names = stored
        with self.repository.connection_factory() as connection:
            model_available = connection.execute(
                """SELECT 1 FROM providers p JOIN models m ON m.provider_id=p.id
                   WHERE p.id=? AND m.id=?""", (row["provider_id"], row["model_id"])
            ).fetchone() is not None
        available = {entry.get("name") for entry in self.tool_catalog() if isinstance(entry, dict)}
        return {
            "id": row["id"], "name": row["name"], "description": row["description"],
            "provider_id": row["provider_id"], "model_id": row["model_id"],
            "system_instructions": row["system_instructions"],
            "model_parameters": json.loads(row["model_parameters"]), "enabled": bool(row["enabled"]),
            "tool_names": list(tool_names), "model_available": model_available,
            "unavailable_tools": sorted(set(tool_names) - available),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def list(self) -> list[dict[str, Any]]:
        return [self._output(item) for item in self.repository.list()]

    def get(self, profile_id: str) -> dict[str, Any]:
        stored = self.repository.get(profile_id)
        if stored is None:
            raise ProfileNotFoundError(profile_id)
        return self._output(stored)

    def create(self, item: AgentProfileInput) -> dict[str, Any]:
        item = self.validate(item)
        self._validate_references(item)
        return self._output(self.repository.create(item))

    def update(self, profile_id: str, values: dict[str, Any]) -> dict[str, Any]:
        current = self.repository.get(profile_id)
        if current is None:
            raise ProfileNotFoundError(profile_id)
        row, tools = current
        merged = AgentProfileInput(
            values.get("name", row["name"]), values.get("description", row["description"]),
            values.get("provider_id", row["provider_id"]), values.get("model_id", row["model_id"]),
            values.get("system_instructions", row["system_instructions"]), values.get("model_parameters", json.loads(row["model_parameters"])),
            values.get("enabled", bool(row["enabled"])), tuple(values.get("tool_names", tools)),
        )
        merged = self.validate(merged)
        self._validate_references(merged)
        update_values = {"name": merged.name, "description": merged.description, "provider_id": merged.provider_id,
                         "model_id": merged.model_id, "system_instructions": merged.system_instructions,
                         "model_parameters": merged.model_parameters, "enabled": int(merged.enabled), "tool_names": merged.tool_names}
        return self._output(self.repository.update(profile_id, update_values))

    def delete(self, profile_id: str) -> None:
        if not self.repository.delete(profile_id):
            raise ProfileNotFoundError(profile_id)


class AgentProfileResolver:
    """Resolve one persisted profile into an immutable runtime configuration."""

    def __init__(self, repository: AgentProfileRepository) -> None:
        self.repository = repository

    def resolve(self, profile_id: str, runtime_temperature: float | None = None) -> AgentRunConfiguration:
        stored = self.repository.get(profile_id)
        if stored is None:
            raise ProfileNotFoundError(profile_id)
        row, tool_names = stored
        provider_id, model_id = row["provider_id"], row["model_id"]
        if not row["enabled"] or not isinstance(provider_id, str) or not provider_id.strip() or not isinstance(model_id, str) or not model_id.strip() or not isinstance(row["system_instructions"], str):
            raise ProfileResolutionError("agent_profile_invalid")
        try:
            parameters = json.loads(row["model_parameters"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProfileResolutionError("agent_profile_invalid") from exc
        if not isinstance(parameters, dict) or set(parameters) - {"temperature"}:
            raise ProfileResolutionError("agent_profile_invalid")
        temperature = parameters.get("temperature", runtime_temperature)
        if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2):
            raise ProfileResolutionError("agent_profile_invalid")
        if any(not isinstance(tool, str) or len(tool) > 200 or not re.fullmatch(r"[A-Za-z0-9_-]+(?:__[A-Za-z0-9_-]+)*", tool) for tool in tool_names):
            raise ProfileResolutionError("agent_profile_invalid")
        with self.repository.connection_factory() as connection:
            if connection.execute("SELECT 1 FROM providers WHERE id=?", (provider_id,)).fetchone() is None:
                raise ProfileResolutionError("agent_model_unavailable")
            if connection.execute("SELECT 1 FROM models WHERE provider_id=? AND id=?", (provider_id, model_id)).fetchone() is None:
                raise ProfileResolutionError("agent_model_unavailable")
        return AgentRunConfiguration(profile_id, provider_id, model_id, row["system_instructions"], temperature, tool_names)
