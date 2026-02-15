"""
Auth Interface - Abstraction layer for credential providers.

This is the integration seam. nterm implements NtermAuthProvider,
standalone mode uses DialogAuthProvider with the borrowed ConnectDialog.

The telemetry system only sees AuthProvider — it doesn't know or care
where the credentials came from.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import logging

logger = logging.getLogger(__name__)


@dataclass
class DeviceTarget:
    """Minimal device identification for the telemetry system."""
    hostname: str
    port: int = 22
    display_name: str = ""
    vendor: str = ""          # e.g., "cisco_ios_xe", "arista_eos", "juniper_junos"
    tags: list[str] = None

    def __post_init__(self):
        if not self.display_name:
            self.display_name = self.hostname
        if self.tags is None:
            self.tags = []


@dataclass
class SSHCredentials:
    """
    Resolved credentials ready for Paramiko.

    This is the output contract — whatever the auth provider does
    internally, it produces this. The poll engine consumes it.
    """
    hostname: str
    port: int
    username: str

    # Auth method (only one set per instance)
    password: Optional[str] = None
    key_path: Optional[str] = None
    key_data: Optional[str] = None
    key_passphrase: Optional[str] = None
    use_agent: bool = False

    # Jump host (if needed)
    jump_host: Optional[str] = None
    jump_port: int = 22
    jump_username: Optional[str] = None
    jump_password: Optional[str] = None
    jump_key_data: Optional[str] = None
    jump_requires_touch: bool = False

    @property
    def display(self) -> str:
        return f"{self.username}@{self.hostname}:{self.port}"


class AuthProvider(ABC):
    """
    Abstract credential provider.

    Subclass this to plug in different credential sources:
    - NtermAuthProvider: Uses nterm's encrypted vault + resolver
    - DialogAuthProvider: Shows ConnectDialog for manual entry
    - SimpleAuthProvider: Hardcoded creds for testing
    """

    @abstractmethod
    def get_credentials(self, target: DeviceTarget) -> Optional[SSHCredentials]:
        """
        Get SSH credentials for a device.

        Args:
            target: Device to authenticate to

        Returns:
            SSHCredentials if successful, None if cancelled/failed
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this provider is ready (vault unlocked, etc.)."""
        ...


class SimpleAuthProvider(AuthProvider):
    """
    Minimal auth provider for development and testing.

    Uses a single username/password for all devices.
    """

    def __init__(self, username: str, password: str = None, key_path: str = None):
        self._username = username
        self._password = password
        self._key_path = key_path

    def get_credentials(self, target: DeviceTarget) -> SSHCredentials:
        return SSHCredentials(
            hostname=target.hostname,
            port=target.port,
            username=self._username,
            password=self._password,
            key_path=self._key_path,
        )

    def is_available(self) -> bool:
        return True


class NtermAuthProvider(AuthProvider):
    """
    Auth provider that hooks into nterm's credential vault.

    This is the integration point — pass in the CredentialResolver
    from wirlwind.vault.resolver and it handles everything:
    vault unlock, pattern matching, key decryption.
    """

    def __init__(self, credential_resolver):
        """
        Args:
            credential_resolver: wirlwind.vault.resolver.CredentialResolver instance
        """
        self._resolver = credential_resolver

    def get_credentials(self, target: DeviceTarget) -> Optional[SSHCredentials]:
        try:
            profile = self._resolver.resolve_for_device(
                hostname=target.hostname,
                tags=target.tags,
                port=target.port,
            )
            return self._profile_to_creds(profile, target)
        except Exception as e:
            logger.error(f"Vault resolution failed for {target.hostname}: {e}")
            return None

    def get_credentials_by_name(
        self, credential_name: str, target: DeviceTarget
    ) -> Optional[SSHCredentials]:
        """Get credentials using a specific named credential from the vault."""
        try:
            profile = self._resolver.create_profile_for_credential(
                credential_name=credential_name,
                hostname=target.hostname,
                port=target.port,
            )
            return self._profile_to_creds(profile, target)
        except Exception as e:
            logger.error(f"Named credential '{credential_name}' failed: {e}")
            return None

    def is_available(self) -> bool:
        try:
            return self._resolver.is_initialized() and self._resolver.store.is_unlocked
        except Exception:
            return False

    @staticmethod
    def _profile_to_creds(profile, target: DeviceTarget) -> SSHCredentials:
        """Convert nterm's ConnectionProfile to our SSHCredentials."""
        if not profile.auth_methods:
            raise ValueError("No auth methods in profile")

        auth = profile.auth_methods[0]

        creds = SSHCredentials(
            hostname=target.hostname,
            port=target.port,
            username=auth.username,
        )

        # Map auth method
        method_name = auth.method.name if hasattr(auth.method, 'name') else str(auth.method)

        if method_name == "PASSWORD":
            creds.password = auth.password
        elif method_name == "KEY_FILE":
            creds.key_path = auth.key_path
            creds.key_passphrase = auth.key_passphrase
        elif method_name == "KEY_STORED":
            creds.key_data = auth.key_data
            creds.key_passphrase = auth.key_passphrase
        elif method_name == "AGENT":
            creds.use_agent = True

        # Jump host
        if profile.jump_hosts:
            jh = profile.jump_hosts[0]
            creds.jump_host = jh.hostname
            creds.jump_port = jh.port
            creds.jump_requires_touch = jh.requires_touch
            if jh.auth:
                creds.jump_username = jh.auth.username
                if hasattr(jh.auth, 'password'):
                    creds.jump_password = jh.auth.password
                if hasattr(jh.auth, 'key_data'):
                    creds.jump_key_data = jh.auth.key_data

        return creds
