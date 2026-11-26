import platform
import subprocess
from collections.abc import Sequence
from os import PathLike

VIRTIO_WIN_URL = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"

_ARCHITECTURES = {
    "arm64": {
        "family": "arm64",
        "fedora_arch": "aarch64",
        "debian_arch": "arm64",
        "alpine_arch": "aarch64",
        "efi_suffix": "aa64",
        "qemu_system": "qemu-system-aarch64",
        "debian_qemu_pkg": "qemu-system-arm",
        "debian_efi_pkg": "qemu-efi-aarch64",
        "fedora_efi_pkg": "edk2-aarch64",
        "alpine_efi_pkg": "aavmf",
        "libvirt_arch": "aarch64",
        "machine": "virt",
        "win_arch": "arm64",
        "driver_arch": "ARM64",
        "tpm_model": "tpm-tis-device",
    },
    "amd64": {
        "family": "amd64",
        "fedora_arch": "x86_64",
        "debian_arch": "amd64",
        "alpine_arch": "x86_64",
        "efi_suffix": "x64",
        "qemu_system": "qemu-system-x86_64",
        "debian_qemu_pkg": "qemu-system-x86",
        "debian_efi_pkg": "ovmf",
        "fedora_efi_pkg": "edk2-ovmf",
        "alpine_efi_pkg": "ovmf",
        "libvirt_arch": "x86_64",
        "machine": "q35",
        "win_arch": "amd64",
        "driver_arch": "amd64",
        "tpm_model": "tpm-crb",
    },
}

_ARCHITECTURE_ALIASES = {
    "aarch64": "arm64",
    "arm64": "arm64",
    "x86_64": "amd64",
    "amd64": "amd64",
}

_INSTALLER_ARCH_KEYS = (
    "family",
    "fedora_arch",
    "debian_arch",
    "alpine_arch",
    "efi_suffix",
    "qemu_system",
    "debian_qemu_pkg",
    "debian_efi_pkg",
    "fedora_efi_pkg",
    "alpine_efi_pkg",
)

_VM_ARCH_KEYS = ("family", "libvirt_arch", "machine", "win_arch", "driver_arch", "tpm_model")


def _get_architecture_spec() -> dict[str, str]:
    machine = platform.machine().lower()
    family = _ARCHITECTURE_ALIASES.get(machine)
    if family is None:
        raise ValueError(machine)
    return _ARCHITECTURES[family]


def get_installer_arch_spec() -> dict[str, str]:
    spec = _get_architecture_spec()
    return {key: spec[key] for key in _INSTALLER_ARCH_KEYS}


def get_vm_arch_spec() -> dict[str, str]:
    spec = _get_architecture_spec()
    return {key: spec[key] for key in _VM_ARCH_KEYS}


def run_command(
    cmd: Sequence[str | PathLike[str]],
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"[EXEC] {' '.join(map(str, cmd))}")
    return subprocess.run(cmd, check=check, text=True, input=input_text)
