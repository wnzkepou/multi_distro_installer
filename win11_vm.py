#!/usr/bin/env python3
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from multiboot_common import VIRTIO_WIN_URL
from multiboot_common import get_vm_arch_spec as _get_vm_arch_spec
from multiboot_common import run_command as run

LIBVIRT_IMAGES = Path("/var/lib/libvirt/images")
VIRTIO_ISO = LIBVIRT_IMAGES / "virtio-win.iso"


def get_vm_arch_spec() -> dict[str, str]:
    try:
        return _get_vm_arch_spec()
    except ValueError as exc:
        sys.exit(f"Error: Unsupported host CPU architecture '{exc}'.")


def build_autounattend_xml(win_arch: str, driver_arch: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://microsoft.com">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-PnpCustomizationsWinPE" processorArchitecture="{win_arch}" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <DriverPaths>
        <PathAndCredentials wcm:action="add" wcm:keyValue="1">
          <Path>D:\\vioscsi\\w11\\{driver_arch}</Path>
        </PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="2">
          <Path>E:\\vioscsi\\w11\\{driver_arch}</Path>
        </PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="3">
          <Path>D:\\NetKVM\\w11\\{driver_arch}</Path>
        </PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="4">
          <Path>E:\\NetKVM\\w11\\{driver_arch}</Path>
        </PathAndCredentials>
      </DriverPaths>
    </component>
    <component name="Microsoft-Windows-Setup" processorArchitecture="{win_arch}" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassTPMCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>2</Order>
          <Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassSecureBootCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>3</Order>
          <Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassRAMCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
      </RunSynchronous>
      <UserData>
        <AcceptEula>true</AcceptEula>
      </UserData>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="{win_arch}" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
    </component>
  </settings>
</unattend>
"""


def locate_uefi_firmware(family: str) -> tuple[Path, Path]:
    """Finds UEFI Secure Boot CODE and VARS templates for arm64 (AAVMF) or amd64 (OVMF)."""
    if family == "arm64":
        candidates = [
            (
                Path("/usr/share/edk2/aarch64/QEMU_EFI-silent-pflash.qcow2"),
                Path("/usr/share/edk2/aarch64/vars-template-pflash.qcow2"),
            ),
            (
                Path("/usr/share/edk2/aarch64/QEMU_EFI-pflash.raw"),
                Path("/usr/share/edk2/aarch64/vars-template-pflash.raw"),
            ),
            (
                Path("/usr/share/AAVMF/AAVMF_CODE.ms.fd"),
                Path("/usr/share/AAVMF/AAVMF_VARS.ms.fd"),
            ),
            (
                Path("/usr/share/AAVMF/AAVMF_CODE.fd"),
                Path("/usr/share/AAVMF/AAVMF_VARS.fd"),
            ),
            (
                Path("/usr/share/edk2/aarch64/QEMU_EFI-pflash.raw"),
                Path("/usr/share/edk2/aarch64/QEMU_VARS-pflash.raw"),
            ),
        ]
    else:
        candidates = [
            (
                Path("/usr/share/edk2/ovmf/OVMF_CODE.secboot.fd"),
                Path("/usr/share/edk2/ovmf/OVMF_VARS.secboot.fd"),
            ),
            (
                Path("/usr/share/edk2/ovmf/OVMF_CODE_4M.secboot.qcow2"),
                Path("/usr/share/edk2/ovmf/OVMF_VARS_4M.secboot.qcow2"),
            ),
            (
                Path("/usr/share/OVMF/OVMF_CODE_4M.ms.fd"),
                Path("/usr/share/OVMF/OVMF_VARS_4M.ms.fd"),
            ),
            (
                Path("/usr/share/OVMF/OVMF_CODE.secboot.fd"),
                Path("/usr/share/OVMF/OVMF_VARS.secboot.fd"),
            ),
            (
                Path("/usr/share/edk2/x64/OVMF_CODE.secboot.4m.fd"),
                Path("/usr/share/edk2/x64/OVMF_VARS.4m.fd"),
            ),
        ]

    for code, vars_tmpl in candidates:
        if code.exists() and vars_tmpl.exists():
            return code, vars_tmpl
    sys.exit(f"Error: UEFI Secure Boot firmware for {family} not found on host.")


def ensure_libvirt_ready():
    if not LIBVIRT_IMAGES.is_dir():
        sys.exit(f"Error: {LIBVIRT_IMAGES} does not exist. Ensure shared/@libvirt is mounted.")

    if shutil.which("systemctl"):
        run(["systemctl", "enable", "--now", "libvirtd"], check=False)
    elif shutil.which("rc-service"):
        run(["rc-service", "libvirtd", "start"], check=False)

    net_info = subprocess.run(["virsh", "net-info", "default"], capture_output=True, text=True)
    if net_info.returncode == 0:
        if "Active:         no" in net_info.stdout:
            run(["virsh", "net-start", "default"], check=False)
        run(["virsh", "net-autostart", "default"], check=False)


def build_autounattend_iso(vm_name: str, win_arch: str, driver_arch: str) -> Path | None:
    iso_tool = shutil.which("xorrisofs") or shutil.which("genisoimage") or shutil.which("mkisofs")
    if not iso_tool:
        print("[WARN] Neither xorrisofs, genisoimage, nor mkisofs found. Skipping autounattend.iso generation.")
        return None

    staging = Path(f"/tmp/{vm_name}_unattend")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    xml_content = build_autounattend_xml(win_arch, driver_arch)
    (staging / "autounattend.xml").write_text(xml_content, encoding="utf-8")

    out_iso = LIBVIRT_IMAGES / f"{vm_name}-unattend.iso"
    run([iso_tool, "-J", "-r", "-V", "UNATTEND", "-o", str(out_iso), str(staging)])
    shutil.rmtree(staging, ignore_errors=True)
    return out_iso


def register_existing_vm(vm_name: str):
    xml_backup = LIBVIRT_IMAGES / f"{vm_name}.xml"
    if not xml_backup.exists():
        sys.exit(f"Error: Portable VM definition {xml_backup} not found.")

    ensure_libvirt_ready()

    shared_vars = LIBVIRT_IMAGES / f"{vm_name}_VARS.fd"
    local_nvram_dir = Path("/var/lib/libvirt/qemu/nvram")
    local_nvram_dir.mkdir(parents=True, exist_ok=True)
    local_vars = local_nvram_dir / f"{vm_name}_VARS.fd"
    if shared_vars.exists() and not local_vars.exists():
        shutil.copy2(shared_vars, local_vars)

    run(["virsh", "define", str(xml_backup)])
    print(f"[OK] Registered '{vm_name}' into current host libvirtd from {xml_backup}.")


def provision_windows_vm(
    vm_name: str,
    win11_iso: Path,
    disk_gb: int,
    ram_mb: int,
    vcpus: int,
):
    arch = get_vm_arch_spec()
    if not win11_iso.exists():
        sys.exit(f"Error: Windows 11 ({arch['family']}) ISO not found at {win11_iso}")

    if not shutil.which("virt-install"):
        sys.exit("Error: 'virt-install' not found. Install virt-install / virtinst on this host.")

    ensure_libvirt_ready()

    if not VIRTIO_ISO.is_file() or VIRTIO_ISO.stat().st_size == 0:
        print("--- Downloading virtio-win.iso into shared/@libvirt ---")
        partial_iso = VIRTIO_ISO.with_name(f"{VIRTIO_ISO.name}.part")
        run(["curl", "-L", "--fail", "--continue-at", "-", "-o", str(partial_iso), VIRTIO_WIN_URL])
        if not partial_iso.is_file() or partial_iso.stat().st_size == 0:
            raise RuntimeError(f"Download did not produce a non-empty ISO: {VIRTIO_ISO}")
        partial_iso.replace(VIRTIO_ISO)

    code_fw, vars_tmpl = locate_uefi_firmware(arch["family"])

    disk_path = LIBVIRT_IMAGES / f"{vm_name}.qcow2"
    shared_vars = LIBVIRT_IMAGES / f"{vm_name}_VARS.fd"

    if disk_path.exists():
        sys.exit(f"Error: Disk image {disk_path} already exists. Use --register-only or choose a new name.")

    shutil.copy2(vars_tmpl, shared_vars)

    print(f"--- Creating {disk_gb}GiB NVMe-tuned qcow2 disk on shared/@libvirt (nodatacow) ---")
    run([
        "qemu-img", "create", "-f", "qcow2",
        "-o", "preallocation=metadata,lazy_refcounts=on",
        str(disk_path), f"{disk_gb}G",
    ])

    unattend_iso = build_autounattend_iso(vm_name, arch["win_arch"], arch["driver_arch"])
    boot_arg = f"uefi,loader={code_fw},loader.readonly=yes,loader.type=pflash,nvram={shared_vars}"

    cmd = [
        "virt-install",
        "--name", vm_name,
        "--arch", arch["libvirt_arch"],
        "--machine", arch["machine"],
        "--virt-type", "kvm",
        "--cpu", "host-passthrough",
        "--vcpus", str(vcpus),
        "--memory", str(ram_mb),
        "--boot", boot_arg,
        "--tpm", f"backend.type=emulator,backend.version=2.0,model={arch['tpm_model']}",
        "--controller", "type=scsi,model=virtio-scsi",
        "--disk", f"path={disk_path},format=qcow2,bus=scsi,cache=none,io=native,discard=unmap",
        "--disk", f"path={win11_iso},device=cdrom,bus=scsi",
        "--disk", f"path={VIRTIO_ISO},device=cdrom,bus=scsi",
        "--network", "network=default,model=virtio",
        "--graphics", "spice,listen=127.0.0.1",
        "--video", "virtio",
        "--channel", "spicevmc",
        "--input", "type=tablet,bus=usb",
        "--input", "type=keyboard,bus=usb",
        "--osinfo", "win11",
        "--noautoconsole",
    ]
    if unattend_iso and unattend_iso.exists():
        cmd.extend(["--disk", f"path={unattend_iso},device=cdrom,bus=scsi"])

    print(f"\n=== Provisioning Windows 11 ({arch['family']}) KVM Guest '{vm_name}' ===")
    run(cmd)

    xml_backup = LIBVIRT_IMAGES / f"{vm_name}.xml"
    xml_dump = subprocess.check_output(["virsh", "dumpxml", vm_name], text=True)
    xml_backup.write_text(xml_dump, encoding="utf-8")

    print(f"\n=== Windows 11 ({arch['family']}) VM Created ===")
    print(f"  Disk Image : {disk_path} (shared/@libvirt, nodatacow, discard=unmap)")
    print(f"  UEFI NVRAM : {shared_vars}")
    print(f"  Domain XML : {xml_backup}")
    print(f"\nConnect console via: virt-viewer {vm_name}  (or open virt-manager)")
    print(f"When hopping to another distro, run: sudo ./win11_vm.py --name {vm_name} --register-only")


def main():
    if os.geteuid() != 0:
        sys.exit("Run this script as root.")

    arch = get_vm_arch_spec()
    default_name = f"win11-{arch['family']}"
    parser = argparse.ArgumentParser(
        description="Provision or register a Windows 11 (arm64/amd64) KVM guest on shared/@libvirt"
    )
    parser.add_argument("--name", default=default_name, help=f"VM domain name (default: {default_name})")
    parser.add_argument("--iso", type=Path, help=f"Path to Windows 11 ({arch['family']}) installation ISO")
    parser.add_argument("--disk-gb", type=int, default=80, help="Virtual NVMe disk size in GiB (default: 80)")
    parser.add_argument("--ram-mb", type=int, default=8192, help="Guest RAM in MiB (default: 8192)")
    parser.add_argument("--vcpus", type=int, default=4, help="Number of host-passthrough vCPUs (default: 4)")
    parser.add_argument(
        "--register-only",
        action="store_true",
        help="Register an existing VM from /var/lib/libvirt/images/.xml into the current host OS",
    )
    args = parser.parse_args()

    if args.register_only:
        register_existing_vm(args.name)
    else:
        if not args.iso:
            iso_input = input(f"Enter full path to Windows 11 ({arch['family']}) ISO: ").strip()
            args.iso = Path(iso_input)
        provision_windows_vm(args.name, args.iso, args.disk_gb, args.ram_mb, args.vcpus)


if __name__ == "__main__":
    main()
