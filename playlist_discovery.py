#!/usr/bin/env python3
"""Fail-closed discovery state for newly created Spotify playlists."""

from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATE_VERSION = 1
PLAYLIST_URI_PREFIX = "spotify:playlist:"

PlaylistConfig = dict[str, str]


class DiscoveryError(RuntimeError):
    """Raised when library or state data is unsafe to use for discovery."""


@dataclass(frozen=True)
class LibraryPlaylist:
    playlist_id: str
    name: str
    owner_uri: str


@dataclass(frozen=True)
class DiscoveryOutcome:
    playlists: tuple[PlaylistConfig, ...]
    initialized: bool
    discovered: tuple[PlaylistConfig, ...]
    ignored_new: int
    deferred_unknown_owner: int
    inactive: int
    library_playlist_count: int
    recovered_from_backup: bool


def _as_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DiscoveryError(f"{label} is not an object")
    return value


def parse_complete_library(response: Mapping[str, Any]) -> list[LibraryPlaylist]:
    """Return playlists only when Spotify supplied the complete library snapshot."""
    data: Mapping[str, Any] = _as_mapping(response.get("data"), "data")
    me: Mapping[str, Any] = _as_mapping(data.get("me"), "data.me")
    library: Mapping[str, Any] = _as_mapping(me.get("libraryV3"), "data.me.libraryV3")
    items_value: object = library.get("items")
    total_value: object = library.get("totalCount")
    if not isinstance(items_value, list):
        raise DiscoveryError("library items are missing")
    if (
        isinstance(total_value, bool)
        or not isinstance(total_value, int)
        or total_value < 0
    ):
        raise DiscoveryError("library totalCount is invalid")
    if len(items_value) != total_value:
        raise DiscoveryError(
            f"incomplete library snapshot ({len(items_value)}/{total_value} items)"
        )

    playlists: list[LibraryPlaylist] = []
    seen_ids: set[str] = set()
    for index, raw_item in enumerate(items_value):
        item: Mapping[str, Any] = _as_mapping(raw_item, f"library item {index}")
        wrapper: Mapping[str, Any] = _as_mapping(
            item.get("item"), f"library item {index}.item"
        )
        item_data: Mapping[str, Any] = _as_mapping(
            wrapper.get("data"), f"library item {index}.item.data"
        )
        if item_data.get("__typename") != "Playlist":
            continue

        uri_value: object = item_data.get("uri")
        name_value: object = item_data.get("name")
        owner_v2_value: object = item_data.get("ownerV2")
        owner_v2: Mapping[str, Any] = (
            owner_v2_value if isinstance(owner_v2_value, Mapping) else {}
        )
        owner_data_value: object = owner_v2.get("data")
        owner_data: Mapping[str, Any] = (
            owner_data_value if isinstance(owner_data_value, Mapping) else {}
        )
        owner_uri_value: object = owner_data.get("uri")
        if (
            not isinstance(uri_value, str)
            or not uri_value.startswith(PLAYLIST_URI_PREFIX)
            or not uri_value.removeprefix(PLAYLIST_URI_PREFIX)
            or not isinstance(name_value, str)
            or not name_value.strip()
        ):
            raise DiscoveryError(f"playlist {index} has incomplete identity data")

        playlist_id: str = uri_value.removeprefix(PLAYLIST_URI_PREFIX)
        owner_uri: str = (
            owner_uri_value
            if isinstance(owner_uri_value, str)
            and owner_uri_value.startswith("spotify:user:")
            else ""
        )
        if playlist_id in seen_ids:
            raise DiscoveryError(f"duplicate playlist ID in library: {playlist_id}")
        seen_ids.add(playlist_id)
        playlists.append(
            LibraryPlaylist(
                playlist_id=playlist_id,
                name=name_value.strip(),
                owner_uri=owner_uri,
            )
        )
    return playlists


def _folder_name(name: str, playlist_id: str) -> str:
    normalized: str = unicodedata.normalize("NFKD", name)
    ascii_name: str = normalized.encode("ascii", "ignore").decode().casefold()
    slug: str = re.sub(r"[^a-z0-9]+", "_", ascii_name).strip("_")
    safe_slug: str = (slug or "playlist")[:48].rstrip("_")
    return f"{safe_slug}_{playlist_id[:8]}"


def _validated_config(value: object, label: str) -> PlaylistConfig:
    raw: Mapping[str, Any] = _as_mapping(value, label)
    result: PlaylistConfig = {}
    for key in ("id", "name", "folder", "jellyfin_name"):
        field: object = raw.get(key)
        if not isinstance(field, str) or not field.strip():
            raise DiscoveryError(f"{label}.{key} is invalid")
        result[key] = field.strip()
    return result


def _validate_state(raw: object, owner_uri: str) -> dict[str, Any]:
    state: Mapping[str, Any] = _as_mapping(raw, "discovery state")
    if state.get("version") != STATE_VERSION:
        raise DiscoveryError("unsupported discovery state version")
    if state.get("owner_uri") != owner_uri:
        raise DiscoveryError("discovery state owner does not match Mia")

    seen_value: object = state.get("seen_playlist_ids")
    auto_value: object = state.get("auto_playlists")
    if not isinstance(seen_value, list) or not all(
        isinstance(value, str) and value for value in seen_value
    ):
        raise DiscoveryError("seen_playlist_ids is invalid")
    if not isinstance(auto_value, list):
        raise DiscoveryError("auto_playlists is invalid")

    auto_playlists: list[PlaylistConfig] = [
        _validated_config(value, f"auto_playlists[{index}]")
        for index, value in enumerate(auto_value)
    ]
    auto_ids: list[str] = [playlist["id"] for playlist in auto_playlists]
    if len(auto_ids) != len(set(auto_ids)):
        raise DiscoveryError("auto_playlists contains duplicate IDs")

    return {
        "version": STATE_VERSION,
        "owner_uri": owner_uri,
        "seen_playlist_ids": sorted(set(seen_value)),
        "auto_playlists": auto_playlists,
    }


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise DiscoveryError(f"cannot read {path.name}: {type(exc).__name__}") from exc


def load_state(state_path: Path, owner_uri: str) -> tuple[dict[str, Any] | None, bool]:
    """Load primary state, falling back to its last valid atomic-write backup."""
    backup_path: Path = state_path.with_suffix(state_path.suffix + ".bak")
    primary_error: DiscoveryError | None = None
    if state_path.exists():
        try:
            return _validate_state(_read_json(state_path), owner_uri), False
        except DiscoveryError as exc:
            primary_error = exc

    if backup_path.exists():
        try:
            return _validate_state(_read_json(backup_path), owner_uri), True
        except DiscoveryError as backup_error:
            if primary_error is not None:
                raise DiscoveryError(
                    f"primary and backup discovery states are invalid: "
                    f"{primary_error}; {backup_error}"
                ) from backup_error
            raise

    if primary_error is not None:
        raise primary_error
    return None, False


def _write_state(
    state_path: Path, state: Mapping[str, Any], backup_primary: bool
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path = state_path.with_suffix(state_path.suffix + ".tmp")
    backup_path: Path = state_path.with_suffix(state_path.suffix + ".bak")
    try:
        with temporary.open("w") as handle:
            handle.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if backup_primary and state_path.exists():
            shutil.copy2(state_path, backup_path)
        temporary.replace(state_path)
        if not backup_path.exists():
            shutil.copy2(state_path, backup_path)
    finally:
        temporary.unlink(missing_ok=True)


def update_discovery_state(
    state_path: Path,
    library_response: Mapping[str, Any],
    owner_uri: str,
    configured_playlists: Sequence[Mapping[str, Any]],
) -> DiscoveryOutcome:
    """Baseline once, then persist and return only newly seen Mia-owned playlists."""
    library_playlists: list[LibraryPlaylist] = parse_complete_library(library_response)
    if not any(playlist.owner_uri == owner_uri for playlist in library_playlists):
        raise DiscoveryError("complete library contains no Mia-owned playlist")
    current_by_id: dict[str, LibraryPlaylist] = {
        playlist.playlist_id: playlist for playlist in library_playlists
    }
    state: dict[str, Any] | None
    recovered: bool
    state, recovered = load_state(state_path, owner_uri)
    if state is None:
        initial_state: dict[str, Any] = {
            "version": STATE_VERSION,
            "owner_uri": owner_uri,
            "seen_playlist_ids": sorted(current_by_id),
            "auto_playlists": [],
        }
        _write_state(state_path, initial_state, backup_primary=False)
        return DiscoveryOutcome(
            playlists=(),
            initialized=True,
            discovered=(),
            ignored_new=0,
            deferred_unknown_owner=0,
            inactive=0,
            library_playlist_count=len(library_playlists),
            recovered_from_backup=False,
        )

    seen_ids: set[str] = set(state["seen_playlist_ids"])
    auto_playlists: list[PlaylistConfig] = [
        dict(playlist) for playlist in state["auto_playlists"]
    ]
    configured_ids: set[str] = {
        str(playlist.get("id"))
        for playlist in configured_playlists
        if playlist.get("id")
    }
    used_jellyfin_names: set[str] = {
        str(playlist.get("jellyfin_name", playlist.get("name", ""))).casefold()
        for playlist in (*configured_playlists, *auto_playlists)
    }

    new_ids: set[str] = set(current_by_id) - seen_ids
    newly_seen_ids: set[str] = set()
    discovered: list[PlaylistConfig] = []
    ignored_new: int = 0
    deferred_unknown_owner: int = 0
    for playlist_id in sorted(new_ids):
        playlist: LibraryPlaylist = current_by_id[playlist_id]
        if not playlist.owner_uri:
            deferred_unknown_owner += 1
            continue
        newly_seen_ids.add(playlist_id)
        if playlist.owner_uri != owner_uri or playlist_id in configured_ids:
            ignored_new += 1
            continue

        jellyfin_name: str = playlist.name
        if jellyfin_name.casefold() in used_jellyfin_names:
            jellyfin_name = f"{playlist.name} [{playlist_id[:6]}]"
        used_jellyfin_names.add(jellyfin_name.casefold())
        config: PlaylistConfig = {
            "id": playlist_id,
            "name": playlist.name,
            "folder": _folder_name(playlist.name, playlist_id),
            "jellyfin_name": jellyfin_name,
        }
        auto_playlists.append(config)
        discovered.append(config)

    updated_state: dict[str, Any] = {
        "version": STATE_VERSION,
        "owner_uri": owner_uri,
        "seen_playlist_ids": sorted(seen_ids | newly_seen_ids),
        "auto_playlists": auto_playlists,
    }
    if updated_state != state or recovered:
        _write_state(state_path, updated_state, backup_primary=not recovered)

    active: list[PlaylistConfig] = []
    inactive: int = 0
    for auto_playlist in auto_playlists:
        current: LibraryPlaylist | None = current_by_id.get(auto_playlist["id"])
        if current is None or current.owner_uri != owner_uri:
            inactive += 1
            continue
        if auto_playlist["id"] not in configured_ids:
            active.append(dict(auto_playlist))

    return DiscoveryOutcome(
        playlists=tuple(active),
        initialized=False,
        discovered=tuple(discovered),
        ignored_new=ignored_new,
        deferred_unknown_owner=deferred_unknown_owner,
        inactive=inactive,
        library_playlist_count=len(library_playlists),
        recovered_from_backup=recovered,
    )
