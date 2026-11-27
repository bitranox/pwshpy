"""Which CIM/WMI methods ``invoke_cim_method`` may call: a fixed read-only allowlist (pure domain).

WMI marks no method as read-only: a ``Get``/``Is`` name is a convention, not a contract, and the
same classes carry ``Encrypt``, ``ClearOwnership`` or ``Terminate`` beside their readers. So
``invoke_cim_method`` refuses every method this module does not name, before it connects to WMI,
and there is deliberately no switch to call anything else: a state-changing method cannot be
reached by a typo or a guessed name. Adding a method means adding it here, in a reviewed change,
once its documentation and a real call show it changes nothing.

Readers that return secret material are left out on purpose, although they change nothing:
``Win32_EncryptableVolume.GetKeyProtectorNumericalPassword`` (the recovery password),
``GetKeyProtectorExternalKey``, ``GetExternalKeyFromFile`` and ``GetKeyPackage``, and
``Win32_Tpm.GetOwnerAuth``. A caller that needs them should not get them as a side effect of a
fingerprint or inventory read.

Matching ignores case, as WMI does, and accepts the namespace with ``/`` or ``\\`` separators.

Contents:
    * :data:`READ_ONLY_CIM_METHODS` - namespace -> class -> method names, all lower case.
    * :func:`is_read_only_cim_method` - whether a namespace/class/method triple is on the list.
    * :data:`CimArgumentValue` - the value types an in-parameter accepts.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TypeAlias

#: What an in-parameter of ``invoke_cim_method`` may be given; the method's own schema decides
#: whether a value fits (a decimal string is accepted for an integer parameter, ``"true"`` or
#: ``"false"`` for a boolean one).
CimArgumentValue: TypeAlias = str | int | bool

_BITLOCKER_NAMESPACE = "root/cimv2/security/microsoftvolumeencryption"
_TPM_NAMESPACE = "root/cimv2/security/microsofttpm"


def _frozen(table: dict[str, dict[str, frozenset[str]]]) -> Mapping[str, Mapping[str, frozenset[str]]]:
    return MappingProxyType({ns: MappingProxyType(classes) for ns, classes in table.items()})


#: namespace -> class -> method, all lower case (see :func:`is_read_only_cim_method`).
READ_ONLY_CIM_METHODS: Mapping[str, Mapping[str, frozenset[str]]] = _frozen(
    {
        "root/cimv2": {
            # The owner of a process; present on every Windows host (the positive control in tests).
            "win32_process": frozenset({"getowner", "getownersid"}),
        },
        "root/default": {
            # Registry readers on the static StdRegProv class; the writers (Set*, Create*, Delete*)
            # are not listed.
            "stdregprov": frozenset({"getstringvalue", "getdwordvalue", "enumkey", "enumvalues"}),
        },
        _BITLOCKER_NAMESPACE: {
            "win32_encryptablevolume": frozenset(
                {
                    "getconversionstatus",
                    "getlockstatus",
                    "getkeyprotectors",
                    "getkeyprotectortype",
                    "getprotectionstatus",
                    "getencryptionmethod",
                    "getversion",
                }
            ),
        },
        _TPM_NAMESPACE: {
            "win32_tpm": frozenset(
                {
                    "isready",
                    "isowned",
                    "isactivated",
                    "isenabled",
                    "isendorsementkeypairpresent",
                    "getdictionaryattackparameters",
                    "getcaplockoutinfo",
                }
            ),
        },
    }
)


def normalize_namespace(namespace: str) -> str:
    """Return ``namespace`` in the allowlist's key form: ``/`` separators, lower case, no edge slashes.

    Args:
        namespace: A WMI namespace as a caller writes it (``root\\\\cimv2`` or ``ROOT/CIMV2``).

    Returns:
        The comparable form.

    Example:
        >>> normalize_namespace("ROOT\\\\CIMV2\\\\Security\\\\MicrosoftTpm")
        'root/cimv2/security/microsofttpm'
    """
    return namespace.replace("\\", "/").strip("/").lower()


def is_read_only_cim_method(namespace: str, class_name: str, method_name: str) -> bool:
    """Return whether ``class_name.method_name`` in ``namespace`` is on the read-only allowlist.

    Args:
        namespace: The WMI namespace, in any case, with ``/`` or ``\\`` separators.
        class_name: The WMI class, in any case.
        method_name: The method, in any case.

    Returns:
        True when :data:`READ_ONLY_CIM_METHODS` names the method for that class and namespace.

    Example:
        >>> is_read_only_cim_method("root/cimv2/Security/MicrosoftTpm", "Win32_Tpm", "IsReady")
        True
        >>> is_read_only_cim_method("root/cimv2/Security/MicrosoftTpm", "Win32_Tpm", "ClearOwnership")
        False
    """
    classes = READ_ONLY_CIM_METHODS.get(normalize_namespace(namespace), {})
    return method_name.lower() in classes.get(class_name.lower(), frozenset())


__all__ = ["READ_ONLY_CIM_METHODS", "CimArgumentValue", "is_read_only_cim_method", "normalize_namespace"]
