#!/usr/bin/env python3
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import multi_distro_installer as installer
from multiboot_common import run_command as run

POOL_MNT = Path("/mnt/multiboot_maint_pool")
EFI_MNT = Path("/mnt/multiboot_maint_efi")
BOOT_MNT = Path("/mnt/multiboot_maint_boot")


def find_partitions():
    def by_label(label):
        res = subprocess.run(["blkid", "-L", label], capture_output=True, text=True)
        return res.stdout.strip() or None

    p_btrfs = by_label("MULTIBOOT_POOL")
    p_efi = by_label("EFI")
    p_boot = by_label("FEDORA_BOOT")

    if not p_btrfs:
        p_btrfs = input("Could not auto-detect MULTIBOOT_POOL. Enter Btrfs partition (e.g., /dev/nvme0n1p3): ").strip()
    return p_efi, p_boot, p_btrfs


def mount_pool(p_btrfs: str):
    POOL_MNT.mkdir(parents=True, exist_ok=True)
    if not os.path.ismount(POOL_MNT):
        run(["mount", "-o", "subvolid=5", p_btrfs, str(POOL_MNT)])


def unmount_all():
    for m in (POOL_MNT, EFI_MNT, BOOT_MNT):
        if os.path.ismount(m):
            run(["umount", str(m)], check=False)


def list_installed_os_folders() -> list:
    if not POOL_MNT.exists():
        return []
    folders = []
    for item in sorted(POOL_MNT.iterdir()):
        if item.is_dir() and item.name != "shared" and (item / "@").exists():
            folders.append(item.name)
    return folders


def is_os_currently_booted(os_id: str) -> bool:
    mounts = Path("/proc/mounts").read_text()
    return f"subvol=/{os_id}/@" in mounts or f"subvol={os_id}/@" in mounts


def inspect_layout():
    print("\n=== Btrfs Pool Hierarchy (subvolid=5) ===")
    shared_dir = POOL_MNT / "shared"
    if shared_dir.exists():
        print("[shared/]")
        for sub in sorted(shared_dir.iterdir()):
            nocow_tag = " (nodatacow)" if sub.name in ("@swap", "@libvirt") else ""
            print(f"  ├── {sub.name}{nocow_tag}")

    for os_id in list_installed_os_folders():
        os_dir = POOL_MNT / os_id
        active_tag = "  <-- (CURRENTLY BOOTED)" if is_os_currently_booted(os_id) else ""
        print(f"\n[{os_id}/]{active_tag}")
        for sub in sorted(os_dir.iterdir()):
            print(f"  ├── {sub.name}")
        snap_dir = os_dir / "@snapshots"
        if snap_dir.exists():
            snaps = sorted([s.name for s in snap_dir.iterdir() if s.name.isdigit()], key=int)
            print(f"  └── Available Snapper Snapshot IDs: {', '.join(snaps) if snaps else 'None'}")


def rollback_os():
    os_folders = list_installed_os_folders()
    if not os_folders:
        print("No installed OS folders found.")
        return

    print("\nSelect OS folder to roll back:")
    for idx, name in enumerate(os_folders, 1):
        print(f"  [{idx}] {name}")
    choice = input("Choice: ").strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(os_folders)):
        print("Invalid selection.")
        return

    os_id = os_folders[int(choice) - 1]
    os_dir = POOL_MNT / os_id
    snap_root = os_dir / "@snapshots"

    snaps = sorted([s.name for s in snap_root.iterdir() if s.name.isdigit()], key=int)
    if not snaps:
        print(f"No Snapper snapshots found in {os_id}/@snapshots.")
        return

    print(f"\nAvailable snapshots for '{os_id}':")
    for sid in snaps:
        info_xml = snap_root / sid / "info.xml"
        desc = ""
        if info_xml.exists():
            m = re.search(r"<description>(.*?)</description>", info_xml.read_text())
            if m:
                desc = m.group(1)
        print(f"  ID {sid}: {desc}")

    target_id = input("\nEnter Snapshot ID to restore as active '@': ").strip()
    snap_subvol = snap_root / target_id / "snapshot"
    if not snap_subvol.exists():
        print(f"Snapshot {target_id} does not exist.")
        return

    confirm = input(f"Confirm atomic rollback of '{os_id}/@' to Snapshot #{target_id}? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Rollback cancelled.")
        return

    ts = time.strftime("%Y%m%d_%H%M%S")
    active_root = os_dir / "@"
    backup_root = os_dir / f"@_pre_rollback_{ts}"

    active_root.rename(backup_root)
    run(["btrfs", "subvolume", "snapshot", str(snap_subvol), str(active_root)])

    print(f"\nSuccess! '{os_id}/@' rolled back to Snapshot #{target_id}.")
    print(f"Previous root preserved at '{os_id}/@_pre_rollback_{ts}'.")


def wipe_single_os(p_efi: str):
    os_folders = list_installed_os_folders()
    if not os_folders:
        print("No installed OS folders found.")
        return

    print("\nSelect OS folder to WIPE (shared/@projects, shared/@containers, shared/@libvirt remain untouched):")
    for idx, name in enumerate(os_folders, 1):
        print(f"  [{idx}] {name}")
    choice = input("Choice: ").strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(os_folders)):
        print("Invalid selection.")
        return

    os_id = os_folders[int(choice) - 1]
    if is_os_currently_booted(os_id):
        print(f"Error: '{os_id}' is currently booted! Boot into another OS or Live USB to wipe it.")
        return

    confirm = input(f"Type 'DELETE {os_id}' to permanently wipe this OS and its swapfile: ").strip()
    if confirm != f"DELETE {os_id}":
        print("Aborted.")
        return

    installer.wipe_os_folder_subvolumes(POOL_MNT, os_id)

    if p_efi:
        EFI_MNT.mkdir(parents=True, exist_ok=True)
        run(["mount", p_efi, str(EFI_MNT)])
        refind_conf = EFI_MNT / "EFI" / "refind" / "refind.conf"
        if refind_conf.exists():
            text = refind_conf.read_text()
            cleaned = re.sub(
                rf'menuentry ".*?" \{{\s*[^}}]*?subvol={re.escape(os_id)}/@[^}}]*?\}}\s*',
                "",
                text,
                flags=re.DOTALL,
            )
            refind_conf.write_text(cleaned)
            print(f"Removed '{os_id}' boot entry from refind.conf.")
        run(["umount", str(EFI_MNT)])

    print(f"\n'{os_id}' has been completely removed.")


def add_or_reinstall_single_os(p_efi: str, p_boot: str, p_btrfs: str):
    if not (p_efi and p_boot and p_btrfs):
        print("Error: Missing EFI, FEDORA_BOOT, or MULTIBOOT_POOL partition.")
        return

    installer.prepare_installer_cache()
    distro = installer.select_distros(single_mode=True)[0]
    os_id = distro["id"]

    if is_os_currently_booted(os_id):
        print(f"Error: '{os_id}' is currently booted! Boot into another OS or Live USB to reinstall it.")
        return

    if (POOL_MNT / os_id).exists():
        ans = input(f"\n'{os_id}/' already exists. Type 'REINSTALL {os_id}' to wipe & replace it: ").strip()
        if ans != f"REINSTALL {os_id}":
            print("Reinstall cancelled.")
            return
        installer.wipe_os_folder_subvolumes(POOL_MNT, os_id)

    user_cfg = installer.prompt_user_config(include_mok_note=False)
    mok = installer.generate_local_mok()

    installer.ensure_shared_subvolumes(POOL_MNT)
    installer.create_single_os_subvolumes(POOL_MNT, distro)

    installer.bootstrap_distro(p_efi, p_boot, p_btrfs, distro, user_cfg, mok)

    sync_refind_kernels(p_efi, p_boot, p_btrfs)
    print(f"\n=== {distro['name']} ({os_id}/) successfully installed with KVM/libvirt & added to rEFInd! ===")


def sync_refind_kernels(p_efi: str, p_boot: str, p_btrfs: str):
    if not p_efi:
        print("EFI partition not found.")
        return

    uuid_btrfs = subprocess.check_output(["blkid", "-s", "UUID", "-o", "value", p_btrfs], text=True).strip()
    partuuid_btrfs = subprocess.check_output(
        ["blkid", "-s", "PARTUUID", "-o", "value", p_btrfs], text=True
    ).strip()
    partuuid_boot = (
        subprocess.check_output(["blkid", "-s", "PARTUUID", "-o", "value", p_boot], text=True).strip()
        if p_boot else ""
    )
    partuuid_efi = (
        subprocess.check_output(["blkid", "-s", "PARTUUID", "-o", "value", p_efi], text=True).strip()
        if p_efi else ""
    )

    EFI_MNT.mkdir(parents=True, exist_ok=True)
    run(["mount", p_efi, str(EFI_MNT)])

    if p_boot:
        BOOT_MNT.mkdir(parents=True, exist_ok=True)
        run(["mount", p_boot, str(BOOT_MNT)])
        fedora_efi_dir = EFI_MNT / "EFI" / "refind" / "fedora"
        fedora_efi_dir.mkdir(parents=True, exist_ok=True)
        for source in sorted(BOOT_MNT.glob("vmlinuz-*")):
            shutil.copy2(source, fedora_efi_dir / source.name)
        for source in sorted(BOOT_MNT.glob("initramfs-*.img")):
            shutil.copy2(source, fedora_efi_dir / source.name)

    stanzas = [
        "# Synced by Multi-Distro Maintenance Helper",
        "timeout 10",
        "use_nvram false",
        "scanfor manual,external",
        "",
    ]
    distro_names = {distro["id"]: distro["name"] for distro in installer.DISTROS.values()}

    for os_id in list_installed_os_folders():
        root_dir = POOL_MNT / os_id / "@"
        pretty_name = distro_names.get(os_id, os_id.replace("_", " ").title())

        if os_id.startswith("fedora") and p_boot:
            kernels = sorted(BOOT_MNT.glob("vmlinuz-*"))
            initrds = sorted(BOOT_MNT.glob("initramfs-*.img"))
            paired = None
            for kernel in reversed(kernels):
                suffix = kernel.name.removeprefix("vmlinuz-")
                initrd = BOOT_MNT / f"initramfs-{suffix}.img"
                if initrd in initrds:
                    paired = (kernel, initrd)
                    break
            if paired:
                kernel, initrd = paired
                stanzas.extend([
                    f'menuentry "{pretty_name} (Secure Boot)" {{',
                    f"    volume {partuuid_efi}",
                    f"    loader /EFI/refind/fedora/{kernel.name}",
                    f"    initrd /EFI/refind/fedora/{initrd.name}",
                    f'    options "root=UUID={uuid_btrfs} rootflags=subvol={os_id}/@ rw quiet rhgb"',
                    "}",
                    "",
                ])
        elif os_id.startswith("debian"):
            boot_dir = root_dir / "boot"
            kernels = sorted(boot_dir.glob("vmlinuz-*"))
            initrds = sorted(boot_dir.glob("initrd.img-*"))
            if kernels and initrds:
                stanzas.extend([
                    f'menuentry "{pretty_name} (Secure Boot)" {{',
                    f"    volume {partuuid_btrfs}",
                    f"    loader /{os_id}/@/boot/{kernels[-1].name}",
                    f"    initrd /{os_id}/@/boot/{initrds[-1].name}",
                    f'    options "root=UUID={uuid_btrfs} rootflags=subvol={os_id}/@ rootfstype=btrfs rw quiet"',
                    "}",
                    "",
                ])
        elif os_id.startswith("alpine"):
            boot_dir = root_dir / "boot"
            kernels = sorted(boot_dir.glob("vmlinuz-lts*"))
            initrds = sorted(boot_dir.glob("initramfs-lts*"))
            if kernels and initrds:
                ensure_alpine_kernel_signed(root_dir, kernels[-1])
                ensure_alpine_kernel_update_hook(root_dir)
                stanzas.extend([
                    f'menuentry "{pretty_name} (Secure Boot)" {{',
                    f"    volume {partuuid_btrfs}",
                    f"    loader /{os_id}/@/boot/{kernels[-1].name}",
                    f"    initrd /{os_id}/@/boot/{initrds[-1].name}",
                    f'    options "root=UUID={uuid_btrfs} rootflags=subvol={os_id}/@ modules=sd-mod,usb-storage,btrfs rootfstype=btrfs console=tty1 console=ttyS0,115200n8 rw"',
                    "}",
                    "",
                ])

    refind_conf = EFI_MNT / "EFI" / "refind" / "refind.conf"
    refind_conf.parent.mkdir(parents=True, exist_ok=True)
    refind_conf.write_text("\n".join(stanzas))
    print("Updated refind.conf with latest kernel & initrd versions.")

    if p_boot and os.path.ismount(BOOT_MNT):
        run(["umount", str(BOOT_MNT)])
    run(["umount", str(EFI_MNT)])


def ensure_alpine_kernel_signed(root_dir: Path, kernel: Path):
    mok_dir = root_dir / "etc" / "secureboot-mok"
    key = mok_dir / "MOK.key"
    cert = mok_dir / "MOK.crt"
    if not key.is_file() or not cert.is_file():
        raise FileNotFoundError(f"Alpine MOK signing key or certificate is missing from {mok_dir}")
    if shutil.which("sbverify") is None:
        raise FileNotFoundError("sbverify is required; install the sbsigntools package on this system")

    verification = subprocess.run(
        ["sbverify", "--cert", str(cert), str(kernel)],
        capture_output=True,
        text=True,
    )
    if verification.returncode == 0:
        print(f"Alpine kernel already has a valid MOK signature: {kernel}")
        return

    print(f"Signing Alpine kernel with its enrolled MOK: {kernel}")
    installer.sign_alpine_kernel(kernel, key, cert)
    verification = subprocess.run(
        ["sbverify", "--cert", str(cert), str(kernel)],
        capture_output=True,
        text=True,
    )
    if verification.returncode != 0:
        detail = verification.stderr.strip() or verification.stdout.strip()
        raise RuntimeError(f"Alpine kernel signature verification failed for {kernel}: {detail}")


def ensure_alpine_kernel_update_hook(root_dir: Path):
    with installer.chroot_env(root_dir, mount_efivars=False):
        result = installer.chroot_run(
            root_dir,
            ["/sbin/apk", "add", "kernel-hooks", "sbsigntool"],
            check=False,
        )
    if result.returncode != 0:
        print(
            "WARNING: Could not install Alpine kernel-hooks/sbsigntool; "
            "the current kernel is signed, but future kernel updates may not be."
        )
        return
    installer.write_alpine_kernel_hook(root_dir)


def enable_alpine_community_repositories():
    alpine_roots = [
        POOL_MNT / os_id / "@"
        for os_id in list_installed_os_folders()
        if os_id.startswith("alpine")
    ]
    if not alpine_roots:
        print("No installed Alpine systems found.")
        return

    for root_dir in alpine_roots:
        installer.enable_alpine_community_repositories(root_dir)
        with installer.chroot_env(root_dir, mount_efivars=False):
            installer.chroot_run(root_dir, ["/sbin/apk", "update"])
        print(f"Enabled Alpine Community repositories for {root_dir.parent.name}.")


def install_snapd_for_fedora_debian():
    distro_by_id = {distro["id"]: distro for distro in installer.DISTROS.values()}
    os_roots = []
    for os_id in list_installed_os_folders():
        distro = distro_by_id.get(os_id)
        if distro and distro["type"] in ("fedora", "debian"):
            os_roots.append((os_id, POOL_MNT / os_id / "@", distro["type"]))

    if not os_roots:
        print("No installed Fedora or Debian systems found.")
        return

    for os_id, root_dir, distro_type in os_roots:
        print(f"Installing and enabling Snap for {os_id}...")
        with installer.chroot_env(root_dir, mount_run=False, mount_efivars=False):
            if distro_type == "fedora":
                installer.chroot_run(root_dir, ["dnf", "-y", "install", installer.SNAPD_PACKAGE])
            else:
                installer.chroot_run(root_dir, ["apt-get", "update"])
                installer.chroot_run(
                    root_dir,
                    [
                        "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
                        installer.SNAPD_PACKAGE,
                    ],
                )
        installer.configure_snapd(root_dir)
        print(f"Snap enabled for {os_id}; the snapd socket will start on its next boot.")


def install_desktop_store_integrations():
    distro_by_id = {distro["id"]: distro for distro in installer.DISTROS.values()}
    os_roots = []
    for os_id in list_installed_os_folders():
        distro = distro_by_id.get(os_id)
        if distro and distro["type"] in ("fedora", "debian"):
            packages = distro.get("software_center_packages", [])
            os_roots.append((os_id, POOL_MNT / os_id / "@", distro, packages))

    if not os_roots:
        print("No installed Fedora or Debian systems found.")
        return

    for os_id, root_dir, distro, packages in os_roots:
        print(f"Installing desktop store integrations and repository AppStream data for {os_id}...")
        with installer.chroot_env(root_dir, mount_run=False, mount_efivars=False):
            if distro["type"] == "fedora":
                installer.chroot_run(
                    root_dir,
                    [
                        "dnf", "-y", "install", installer.FLATPAK_PACKAGE,
                        installer.SNAPD_PACKAGE, *packages,
                        *installer.RPMFUSION_APPSTREAM_PACKAGES,
                    ],
                )
            else:
                installer.chroot_run(root_dir, ["apt-get", "update"])
                installer.chroot_run(
                    root_dir,
                    [
                        "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
                        installer.FLATPAK_PACKAGE, installer.SNAPD_PACKAGE, *packages,
                    ],
                )
        installer.configure_snapd(root_dir)
        installer.configure_flathub(root_dir)
        print(f"Desktop stores configured for {os_id}.")


def main():
    if os.geteuid() != 0:
        sys.exit("Run this maintenance helper as root.")

    p_efi, p_boot, p_btrfs = find_partitions()
    try:
        mount_pool(p_btrfs)
        while True:
            print("\n=== Multi-Distro Btrfs Rollback & Maintenance Helper ===")
            print("  [1] Inspect Btrfs pool & Snapper snapshots")
            print("  [2] Atomic Snapper rollback (<os_folder>/@)")
            print("  [3] Wipe a single OS (<os_folder> + swapfile + rEFInd entry)")
            print("  [4] Repair Alpine kernel signatures & sync rEFInd entries")
            print("  [5] Add / Reinstall a single OS into existing pool")
            print("  [6] Enable Alpine Community repositories")
            print("  [7] Install / Enable Snap on Fedora and Debian")
            print("  [8] Install desktop store integrations & RPM Fusion AppStream data")
            print("  [9] Exit")

            choice = input("Select action: ").strip()
            if choice == "1":
                inspect_layout()
            elif choice == "2":
                rollback_os()
            elif choice == "3":
                wipe_single_os(p_efi)
            elif choice == "4":
                sync_refind_kernels(p_efi, p_boot, p_btrfs)
            elif choice == "5":
                add_or_reinstall_single_os(p_efi, p_boot, p_btrfs)
            elif choice == "6":
                enable_alpine_community_repositories()
            elif choice == "7":
                install_snapd_for_fedora_debian()
            elif choice == "8":
                install_desktop_store_integrations()
            elif choice == "9":
                break
            else:
                print("Invalid choice.")
    finally:
        unmount_all()


if __name__ == "__main__":
    main()
