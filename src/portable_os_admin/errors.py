"""Exception hierarchy shared across the application."""


class PortableOSAdminError(Exception):
    """Base class for all application-specific errors."""


class ContainerError(PortableOSAdminError):
    """Base class for admin-container related errors."""


class ContainerAuthenticationError(ContainerError):
    """Raised when the supplied container password is incorrect."""


class ContainerCorruptedError(ContainerError):
    """Raised when a container file cannot be parsed or its integrity check fails."""


class ContainerAlreadyExistsError(ContainerError):
    """Raised when attempting to create a container at a path that already has one."""


class WeakPasswordError(ContainerError):
    """Raised when a new container password does not meet the strength policy."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class SessionClosedError(PortableOSAdminError):
    """Raised when an operation is attempted on a session that has already ended."""


class ConfigError(PortableOSAdminError):
    """Base class for install-configuration related errors."""


class ConfigValidationError(ConfigError):
    """Raised when an install configuration fails validation and is used anyway."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class InstallError(PortableOSAdminError):
    """Base class for installation-pipeline errors."""


class DeviceNotEligibleError(InstallError):
    """Raised when the selected external medium fails the pre-install eligibility check."""


class RecoveryError(PortableOSAdminError):
    """Base class for recovery-flow errors."""


class RecoveryRequestInvalidError(RecoveryError):
    """Raised when a recovery request string fails checksum validation."""


class RecoveryDeniedError(RecoveryError):
    """Raised when no usable recovery record exists for a given request."""


class MediaNotRegisteredError(PortableOSAdminError):
    """Raised when the connected medium has no matching record in the admin container."""


class ExchangeError(PortableOSAdminError):
    """Base class for exchange-partition errors."""


class InsufficientSpaceError(ExchangeError):
    """Raised when a write to the exchange partition would exceed free space."""


class LogsError(PortableOSAdminError):
    """Base class for log-partition errors."""


class LogsUnavailableError(LogsError):
    """Raised when the log partition cannot be opened/mounted."""


class LogsCorruptedError(LogsError):
    """Raised when log files are missing or unreadable."""
