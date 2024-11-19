"""Backup manager for the Backup integration."""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
from queue import SimpleQueue
import shutil
import tarfile
from tempfile import TemporaryDirectory
import time
from typing import Any, Protocol

import aiohttp
from securetar import SecureTarFile, atomic_contents_add

from homeassistant.backup_restore import RESTORE_BACKUP_FILE, password_to_key
from homeassistant.const import __version__ as HAVERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import integration_platform
from homeassistant.helpers.json import json_bytes
from homeassistant.util import dt as dt_util

from .agent import (
    BackupAgent,
    BackupAgentError,
    BackupAgentPlatformProtocol,
    LocalBackupAgent,
)
from .config import BackupConfig
from .const import (
    BUF_SIZE,
    DATA_MANAGER,
    DOMAIN,
    EXCLUDE_DATABASE_FROM_BACKUP,
    EXCLUDE_FROM_BACKUP,
    LOGGER,
)
from .models import BackupUploadMetadata, BaseBackup
from .util import read_backup


@dataclass(slots=True)
class NewBackup:
    """New backup class."""

    backup_id: str


@dataclass(slots=True)
class Backup(BaseBackup):
    """Backup class."""

    agent_ids: list[str]


@dataclass(slots=True)
class BackupProgress:
    """Backup progress class."""

    done: bool
    stage: str | None
    success: bool | None


class BackupPlatformProtocol(Protocol):
    """Define the format that backup platforms can have."""

    async def async_pre_backup(self, hass: HomeAssistant) -> None:
        """Perform operations before a backup starts."""

    async def async_post_backup(self, hass: HomeAssistant) -> None:
        """Perform operations after a backup finishes."""


class BackupReaderWriter(abc.ABC):
    """Abstract class for reading and writing backups."""

    temp_backup_dir: Path

    @abc.abstractmethod
    async def async_create_backup(
        self,
        *,
        addons_included: list[str] | None,
        agent_ids: list[str],
        database_included: bool,
        backup_name: str,
        folders_included: list[str] | None,
        on_progress: Callable[[BackupProgress], None] | None,
        password: str | None,
    ) -> tuple[NewBackup, asyncio.Task[BaseBackup]]:
        """Create a backup."""

    @abc.abstractmethod
    async def async_restore_backup(
        self,
        backup_id: str,
        *,
        agent_id: str,
        password: str | None,
        **kwargs: Any,
    ) -> None:
        """Restore a backup."""


class BackupManager:
    """Define the format that backup managers can have."""

    def __init__(self, hass: HomeAssistant, reader_writer: BackupReaderWriter) -> None:
        """Initialize the backup manager."""
        self.hass = hass
        self.backup_task: asyncio.Task | None = None
        self.platforms: dict[str, BackupPlatformProtocol] = {}
        self.backup_agents: dict[str, BackupAgent] = {}
        self.local_backup_agents: dict[str, LocalBackupAgent] = {}
        self.config = BackupConfig(hass)
        self.syncing = False
        self._reader_writer = reader_writer

    async def async_setup(self) -> None:
        """Set up the backup manager."""
        await self.config.load()
        await self.load_platforms()

    @property
    def temp_backup_dir(self) -> Path:
        """Return the temporary backup directory."""
        return self._reader_writer.temp_backup_dir

    @callback
    def _add_platform_pre_post_handler(
        self,
        integration_domain: str,
        platform: BackupPlatformProtocol,
    ) -> None:
        """Add a backup platform."""
        if not hasattr(platform, "async_pre_backup") or not hasattr(
            platform, "async_post_backup"
        ):
            return

        self.platforms[integration_domain] = platform

    async def _async_add_platform_agents(
        self,
        integration_domain: str,
        platform: BackupAgentPlatformProtocol,
    ) -> None:
        """Add a platform to the backup manager."""
        if not hasattr(platform, "async_get_backup_agents"):
            return

        agents = await platform.async_get_backup_agents(self.hass)
        self.backup_agents.update(
            {f"{integration_domain}.{agent.name}": agent for agent in agents}
        )
        self.local_backup_agents.update(
            {
                f"{integration_domain}.{agent.name}": agent
                for agent in agents
                if isinstance(agent, LocalBackupAgent)
            }
        )

    async def _add_platform(
        self,
        hass: HomeAssistant,
        integration_domain: str,
        platform: Any,
    ) -> None:
        """Add a backup platform manager."""
        self._add_platform_pre_post_handler(integration_domain, platform)
        await self._async_add_platform_agents(integration_domain, platform)

    async def async_pre_backup_actions(self, **kwargs: Any) -> None:
        """Perform pre backup actions."""
        pre_backup_results = await asyncio.gather(
            *(
                platform.async_pre_backup(self.hass)
                for platform in self.platforms.values()
            ),
            return_exceptions=True,
        )
        for result in pre_backup_results:
            if isinstance(result, Exception):
                raise result

    async def async_post_backup_actions(self, **kwargs: Any) -> None:
        """Perform post backup actions."""
        post_backup_results = await asyncio.gather(
            *(
                platform.async_post_backup(self.hass)
                for platform in self.platforms.values()
            ),
            return_exceptions=True,
        )
        for result in post_backup_results:
            if isinstance(result, Exception):
                raise result

    async def load_platforms(self) -> None:
        """Load backup platforms."""
        await integration_platform.async_process_integration_platforms(
            self.hass,
            DOMAIN,
            self._add_platform,
            wait_for_platforms=True,
        )
        LOGGER.debug("Loaded %s platforms", len(self.platforms))
        LOGGER.debug("Loaded %s agents", len(self.backup_agents))

    async def _async_upload_backup(
        self,
        *,
        backup: BaseBackup,
        agent_ids: list[str],
        path: Path,
    ) -> None:
        """Upload a backup to selected agents."""
        self.syncing = True
        try:
            sync_backup_results = await asyncio.gather(
                *(
                    self.backup_agents[agent_id].async_upload_backup(
                        path=path,
                        metadata=BackupUploadMetadata(
                            backup_id=backup.backup_id,
                            date=backup.date,
                            homeassistant=HAVERSION,
                            name=backup.name,
                            protected=backup.protected,
                            size=backup.size,
                        ),
                    )
                    for agent_id in agent_ids
                ),
                return_exceptions=True,
            )
            for result in sync_backup_results:
                if isinstance(result, Exception):
                    LOGGER.exception(
                        "Error during backup upload - %s", result, exc_info=result
                    )
        finally:
            self.syncing = False

    async def async_get_backups(
        self, **kwargs: Any
    ) -> tuple[dict[str, Backup], dict[str, Exception]]:
        """Get backups.

        Return a dictionary of Backup instances keyed by their ID.
        """
        backups: dict[str, Backup] = {}
        agent_errors: dict[str, Exception] = {}
        agent_ids = list(self.backup_agents)

        list_backups_results = await asyncio.gather(
            *(agent.async_list_backups() for agent in self.backup_agents.values()),
            return_exceptions=True,
        )
        for idx, result in enumerate(list_backups_results):
            if isinstance(result, BackupAgentError):
                agent_errors[agent_ids[idx]] = result
                continue
            if isinstance(result, BaseException):
                raise result
            for agent_backup in result:
                if agent_backup.backup_id not in backups:
                    backups[agent_backup.backup_id] = Backup(
                        agent_ids=[],
                        backup_id=agent_backup.backup_id,
                        date=agent_backup.date,
                        name=agent_backup.name,
                        protected=agent_backup.protected,
                        size=agent_backup.size,
                    )
                backups[agent_backup.backup_id].agent_ids.append(agent_ids[idx])

        return (backups, agent_errors)

    async def async_get_backup(
        self, backup_id: str, **kwargs: Any
    ) -> tuple[Backup | None, dict[str, Exception]]:
        """Get a backup."""
        backup: Backup | None = None
        agent_errors: dict[str, Exception] = {}
        agent_ids = list(self.backup_agents.keys())

        get_backup_results = await asyncio.gather(
            *(
                agent.async_get_backup(backup_id)
                for agent in self.backup_agents.values()
            ),
            return_exceptions=True,
        )
        for idx, result in enumerate(get_backup_results):
            if isinstance(result, BackupAgentError):
                agent_errors[agent_ids[idx]] = result
                continue
            if isinstance(result, BaseException):
                raise result
            if not result:
                continue
            if backup is None:
                backup = Backup(
                    agent_ids=[],
                    backup_id=result.backup_id,
                    date=result.date,
                    name=result.name,
                    protected=result.protected,
                    size=result.size,
                )
                backup.agent_ids.append(agent_ids[idx])

        return (backup, agent_errors)

    async def async_remove_backup(self, backup_id: str, **kwargs: Any) -> None:
        """Remove a backup."""
        for agent in self.backup_agents.values():
            await agent.async_delete_backup(backup_id)

    async def async_receive_backup(
        self,
        *,
        agent_ids: list[str],
        contents: aiohttp.BodyPartReader,
        **kwargs: Any,
    ) -> None:
        """Receive and store a backup file from upload."""
        queue: SimpleQueue[tuple[bytes, asyncio.Future[None] | None] | None] = (
            SimpleQueue()
        )
        temp_dir_handler = await self.hass.async_add_executor_job(TemporaryDirectory)
        target_temp_file = Path(
            temp_dir_handler.name, contents.filename or "backup.tar"
        )

        def _sync_queue_consumer() -> None:
            with target_temp_file.open("wb") as file_handle:
                while True:
                    if (_chunk_future := queue.get()) is None:
                        break
                    _chunk, _future = _chunk_future
                    if _future is not None:
                        self.hass.loop.call_soon_threadsafe(_future.set_result, None)
                    file_handle.write(_chunk)

        fut: asyncio.Future[None] | None = None
        try:
            fut = self.hass.async_add_executor_job(_sync_queue_consumer)
            megabytes_sending = 0
            while chunk := await contents.read_chunk(BUF_SIZE):
                megabytes_sending += 1
                if megabytes_sending % 5 != 0:
                    queue.put_nowait((chunk, None))
                    continue

                chunk_future = self.hass.loop.create_future()
                queue.put_nowait((chunk, chunk_future))
                await asyncio.wait(
                    (fut, chunk_future),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if fut.done():
                    # The executor job failed
                    break

            queue.put_nowait(None)  # terminate queue consumer
        finally:
            if fut is not None:
                await fut

        def _copy_and_cleanup(local_file_paths: list[Path], backup: BaseBackup) -> Path:
            if local_file_paths:
                tar_file_path = local_file_paths[0]
            else:
                tar_file_path = self.temp_backup_dir / f"{backup.backup_id}.tar"
            for local_path in local_file_paths:
                shutil.copy(target_temp_file, local_path)
            temp_dir_handler.cleanup()
            return tar_file_path

        try:
            backup = await self.hass.async_add_executor_job(
                read_backup, target_temp_file
            )
        except (OSError, tarfile.TarError, json.JSONDecodeError, KeyError) as err:
            LOGGER.warning("Unable to parse backup %s: %s", target_temp_file, err)
            return

        local_file_paths = [
            self.local_backup_agents[agent_id].get_backup_path(backup.backup_id)
            for agent_id in agent_ids
            if agent_id in self.local_backup_agents
        ]
        tar_file_path = await self.hass.async_add_executor_job(
            _copy_and_cleanup, local_file_paths, backup
        )
        await self._async_upload_backup(
            backup=backup, agent_ids=agent_ids, path=tar_file_path
        )
        if not local_file_paths:
            await self.hass.async_add_executor_job(tar_file_path.unlink, True)

    async def async_create_backup(
        self,
        *,
        addons_included: list[str] | None,
        agent_ids: list[str],
        database_included: bool,
        folders_included: list[str] | None,
        name: str | None,
        on_progress: Callable[[BackupProgress], None] | None,
        password: str | None,
        **kwargs: Any,
    ) -> NewBackup:
        """Initiate generating a backup.

        :param on_progress: A callback that will be called with the progress of the
            backup.
        """
        if self.backup_task:
            raise HomeAssistantError("Backup already in progress")
        if not agent_ids:
            raise HomeAssistantError("At least one agent must be selected")
        if any(agent_id not in self.backup_agents for agent_id in agent_ids):
            raise HomeAssistantError("Invalid agent selected")
        backup_name = name or f"Core {HAVERSION}"
        new_backup, self.backup_task = await self._reader_writer.async_create_backup(
            addons_included=addons_included,
            agent_ids=agent_ids,
            backup_name=backup_name,
            database_included=database_included,
            folders_included=folders_included,
            on_progress=on_progress,
            password=password,
        )
        self.backup_task.add_done_callback(lambda _: setattr(self, "backup_task", None))
        return new_backup

    async def async_restore_backup(
        self,
        backup_id: str,
        *,
        agent_id: str,
        password: str | None,
        **kwargs: Any,
    ) -> None:
        """Initiate restoring a backup.

        :param on_progress: A callback that will be called with the progress of the
            restore. Home Assistant Core may need to be restarted during the backup
            restore process, which means the restore process may not be able to report
            when it's done.
        """

        if agent_id in self.local_backup_agents:
            local_agent = self.local_backup_agents[agent_id]
            if not await local_agent.async_get_backup(backup_id):
                raise HomeAssistantError(
                    f"Backup {backup_id} not found in agent {agent_id}"
                )
        else:
            path = self.temp_backup_dir / f"{backup_id}.tar"
            agent = self.backup_agents[agent_id]
            if not await agent.async_get_backup(backup_id):
                raise HomeAssistantError(
                    f"Backup {backup_id} not found in agent {agent_id}"
                )
            await agent.async_download_backup(backup_id, path=path)

        await self._reader_writer.async_restore_backup(
            backup_id=backup_id,
            agent_id=agent_id,
            password=password,
        )


class CoreBackupReaderWriter(BackupReaderWriter):
    """Class for reading and writing backups in core and container installations."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the backup reader/writer."""
        self._hass = hass
        self.temp_backup_dir = Path(hass.config.path("tmp_backups"))

    async def async_create_backup(
        self,
        *,
        addons_included: list[str] | None,
        agent_ids: list[str],
        database_included: bool,
        backup_name: str,
        folders_included: list[str] | None,
        on_progress: Callable[[BackupProgress], None] | None,
        password: str | None,
    ) -> tuple[NewBackup, asyncio.Task[BaseBackup]]:
        """Initiate generating a backup."""
        date_str = dt_util.now().isoformat()
        backup_id = _generate_backup_id(date_str, backup_name)

        backup_task = self._hass.async_create_task(
            self._async_create_backup(
                addons_included=addons_included,
                agent_ids=agent_ids,
                backup_id=backup_id,
                backup_name=backup_name,
                database_included=database_included,
                date_str=date_str,
                folders_included=folders_included,
                on_progress=on_progress,
                password=password,
            ),
            name="backup_manager_create_backup",
            eager_start=False,  # To ensure the task is not started before we return
        )

        return (NewBackup(backup_id=backup_id), backup_task)

    async def _async_create_backup(
        self,
        *,
        addons_included: list[str] | None,
        agent_ids: list[str],
        database_included: bool,
        backup_name: str,
        date_str: str,
        folders_included: list[str] | None,
        on_progress: Callable[[BackupProgress], None] | None,
        password: str | None,
        backup_id: str,
    ) -> BaseBackup:
        """Generate a backup."""
        manager = self._hass.data[DATA_MANAGER]
        success = False

        local_file_paths = [
            manager.local_backup_agents[agent_id].get_backup_path(backup_id)
            for agent_id in agent_ids
            if agent_id in manager.local_backup_agents
        ]

        try:
            # Inform integrations a backup is about to be made
            await manager.async_pre_backup_actions()

            backup_data = {
                "compressed": True,
                "date": date_str,
                "folders": ["homeassistant"],
                "homeassistant": {
                    "exclude_database": not database_included,
                    "version": HAVERSION,
                },
                "name": backup_name,
                "protected": password is not None,
                "slug": backup_id,
                "type": "partial",
            }

            tar_file_path, size_in_bytes = await self._hass.async_add_executor_job(
                self._mkdir_and_generate_backup_contents,
                local_file_paths,
                backup_data,
                database_included,
                password,
            )
            backup = BaseBackup(
                backup_id=backup_id,
                date=date_str,
                name=backup_name,
                protected=password is not None,
                size=round(size_in_bytes / 1_048_576, 2),
            )
            LOGGER.debug(
                "Generated new backup with backup_id %s, uploading to agents %s",
                backup_id,
                agent_ids,
            )
            await manager._async_upload_backup(  # noqa: SLF001
                backup=backup, agent_ids=agent_ids, path=tar_file_path
            )
            if not local_file_paths:
                await self._hass.async_add_executor_job(tar_file_path.unlink, True)
            success = True
            return backup
        finally:
            if on_progress:
                on_progress(BackupProgress(done=True, stage=None, success=success))
            # Inform integrations the backup is done
            await manager.async_post_backup_actions()

    def _mkdir_and_generate_backup_contents(
        self,
        tar_file_paths: list[Path],
        backup_data: dict[str, Any],
        database_included: bool,
        password: str | None,
    ) -> tuple[Path, int]:
        """Generate backup contents and return the size."""
        if tar_file_paths:
            tar_file_path = tar_file_paths[0]
        else:
            tar_file_path = self.temp_backup_dir / f"{backup_data['slug']}.tar"
        if not (backup_dir := tar_file_path.parent).exists():
            LOGGER.debug("Creating backup directory %s", backup_dir)
            backup_dir.mkdir()

        excludes = EXCLUDE_FROM_BACKUP
        if not database_included:
            excludes = excludes + EXCLUDE_DATABASE_FROM_BACKUP

        outer_secure_tarfile = SecureTarFile(
            tar_file_path, "w", gzip=False, bufsize=BUF_SIZE
        )
        with outer_secure_tarfile as outer_secure_tarfile_tarfile:
            raw_bytes = json_bytes(backup_data)
            fileobj = io.BytesIO(raw_bytes)
            tar_info = tarfile.TarInfo(name="./backup.json")
            tar_info.size = len(raw_bytes)
            tar_info.mtime = int(time.time())
            outer_secure_tarfile_tarfile.addfile(tar_info, fileobj=fileobj)
            with outer_secure_tarfile.create_inner_tar(
                "./homeassistant.tar.gz",
                gzip=True,
                key=password_to_key(password) if password is not None else None,
            ) as core_tar:
                atomic_contents_add(
                    tar_file=core_tar,
                    origin_path=Path(self._hass.config.path()),
                    excludes=excludes,
                    arcname="data",
                )
        for local_path in tar_file_paths[1:]:
            shutil.copy(tar_file_path, local_path)
        return (tar_file_path, tar_file_path.stat().st_size)

    async def async_restore_backup(
        self,
        backup_id: str,
        *,
        agent_id: str,
        password: str | None,
        **kwargs: Any,
    ) -> None:
        """Restore a backup.

        This will write the restore information to .HA_RESTORE which
        will be handled during startup by the restore_backup module.
        """

        manager = self._hass.data[DATA_MANAGER]
        if agent_id in manager.local_backup_agents:
            local_agent = manager.local_backup_agents[agent_id]
            path = local_agent.get_backup_path(backup_id)
        else:
            path = self.temp_backup_dir / f"{backup_id}.tar"

        def _write_restore_file() -> None:
            """Write the restore file."""
            Path(self._hass.config.path(RESTORE_BACKUP_FILE)).write_text(
                json.dumps({"path": path.as_posix(), "password": password}),
                encoding="utf-8",
            )

        await self._hass.async_add_executor_job(_write_restore_file)
        await self._hass.services.async_call("homeassistant", "restart", {})


def _generate_backup_id(date: str, name: str) -> str:
    """Generate a backup ID."""
    return hashlib.sha1(f"{date} - {name}".lower().encode()).hexdigest()[:8]
