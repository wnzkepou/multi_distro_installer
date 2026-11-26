#!/usr/bin/env python3
import errno
import getpass
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import NotRequired, TypedDict

from multiboot_common import (
    FLATHUB_REMOTE_NAME,
    FLATHUB_REMOTE_URL,
    FLATPAK_PACKAGE,
    SNAP_CLASSIC_PATH,
    SNAPD_PACKAGE,
    SNAPD_SOCKET,
    VIRTIO_WIN_URL,
    get_installer_arch_spec,
)
from multiboot_common import run_command as run

CACHE_DIR = Path("/tmp/multiboot_installer_cache")
LEGACY_CACHE_DIR = Path("/var/cache/multiboot_installer")
REFIND_CACHE_DIR = Path("/var/tmp/multiboot_installer_cache")
MOK_DIR = CACHE_DIR / "mok"
MOUNT_ROOT = Path("/mnt/multiboot_target")
BTRFS_OPTS = "defaults,noatime,compress=zstd:1,space_cache=v2"
BTRFS_NOCOW_OPTS = "defaults,noatime,nodatacow"
REFIND_VERSION = "0.14.2"
REFIND_ZIP_URL = f"https://sourceforge.net/projects/refind/files/{REFIND_VERSION}/refind-bin-{REFIND_VERSION}.zip/download"
ALPINE_BRANCH = "v3.24"
ALPINE_VERSION = "3.24.4"
EFI_PARTITION_END_MIB = 1025
BOOT_PARTITION_END_MIB = 5121


class DistroConfig(TypedDict):
    id: str
    name: str
    type: str
    desktop: str
    initial_packages: NotRequired[list[str]]
    dm: str | None
    dm_subvol: str | None
    dm_mount: str | None
    swap_gb: int


DISTROS: dict[str, DistroConfig] = {
    "1": {
        "id": "fedora_kde",
        "name": "Fedora KDE",
        "type": "fedora",
        "desktop": "@kde-desktop-environment",
        "dm": "sddm",
        "dm_subvol": "@sddm",
        "dm_mount": "/var/lib/sddm",
        "swap_gb": 8,
    },
    "2": {
        "id": "fedora_gnome",
        "name": "Fedora GNOME",
        "type": "fedora",
        "desktop": "@workstation-product-environment",
        "initial_packages": ["gparted"],
        "dm": "gdm",
        "dm_subvol": "@gdm",
        "dm_mount": "/var/lib/gdm",
        "swap_gb": 8,
    },
    "3": {
        "id": "debian_gnome",
        "name": "Debian GNOME",
        "type": "debian",
        "desktop": "task-gnome-desktop",
        "initial_packages": ["gparted"],
        "dm": "gdm3",
        "dm_subvol": "@gdm3",
        "dm_mount": "/var/lib/gdm3",
        "swap_gb": 8,
    },
    "4": {
        "id": "debian_kde",
        "name": "Debian KDE",
        "type": "debian",
        "desktop": "task-kde-desktop",
        "dm": "sddm",
        "dm_subvol": "@sddm",
        "dm_mount": "/var/lib/sddm",
        "swap_gb": 8,
    },
    "5": {
        "id": "alpine_xfce",
        "name": "Alpine XFCE",
        "type": "alpine",
        "desktop": "xfce4 xfce4-terminal lightdm lightdm-gtk-greeter",
        "initial_packages": [
            "dbus-openrc",
            "elogind",
            "elogind-openrc",
            "lightdm-openrc",
            "xorg-server",
        ],
        "dm": "lightdm",
        "dm_subvol": None,
        "dm_mount": None,
        "swap_gb": 4,
    },
    "6": {
        "id": "alpine_sway",
        "name": "Alpine Sway",
        "type": "alpine",
        "desktop": "sway swaylock swayidle foot waybar wmenu seatd",
        "dm": None,
        "dm_subvol": None,
        "dm_mount": None,
        "swap_gb": 4,
    },
}


def get_arch_spec() -> dict[str, str]:
    try:
        return get_installer_arch_spec()
    except ValueError as exc:
        raw = str(exc)
        sys.exit(f"Error: Unsupported CPU architecture '{raw}'. Supported: arm64/aarch64, amd64/x86_64.")


def get_dm_subvolume_config(distro: DistroConfig) -> tuple[str, str] | None:
    dm_subvol = distro["dm_subvol"]
    dm_mount = distro["dm_mount"]
    if dm_subvol is None and dm_mount is None:
        return None
    if dm_subvol is None or dm_mount is None:
        raise ValueError(f"Incomplete display-manager mount configuration for '{distro['id']}'.")
    return dm_subvol, dm_mount


def get_rpmfusion_release_packages() -> list[str]:
    release = subprocess.run(
        ["rpm", "-E", "%fedora"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not release.isdigit():
        raise RuntimeError(f"Unable to determine Fedora release version from rpm: {release!r}")

    return [
        f"https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-{release}.noarch.rpm",
        f"https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-{release}.noarch.rpm",
    ]


def set_target_passwords(target: Path, username: str, password: str):
    if username == "root":
        raise ValueError("The developer username cannot be 'root'.")
    shadow_file = target / "etc" / "shadow"
    if shadow_file.is_symlink() or not shadow_file.is_file():
        raise RuntimeError(f"Target shadow password file is missing or invalid: {shadow_file}")

    def hash_password() -> str:
        result = subprocess.run(
            ["openssl", "passwd", "-6", "-stdin"],
            check=True,
            capture_output=True,
            text=True,
            input=f"{password}\n",
        )
        password_hash = result.stdout.strip()
        if not password_hash.startswith("$6$"):
            raise RuntimeError("OpenSSL did not produce a SHA-512 crypt password hash.")
        return password_hash

    hashes = {"root": hash_password(), username: hash_password()}
    changed = set()
    today = str(int(time.time() // 86400))
    lines = shadow_file.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        fields = line.split(":")
        if fields[0] in hashes:
            if len(fields) < 9:
                raise RuntimeError(f"Malformed shadow entry for {fields[0]} in {shadow_file}")
            fields[1] = hashes[fields[0]]
            fields[2] = today
            lines[index] = ":".join(fields)
            changed.add(fields[0])

    missing = hashes.keys() - changed
    if missing:
        raise RuntimeError(f"Missing shadow entries in {shadow_file}: {', '.join(sorted(missing))}")

    metadata = shadow_file.stat()
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=shadow_file.parent, prefix=".shadow.", delete=False
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write("\n".join(lines) + "\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chown(temp_path, metadata.st_uid, metadata.st_gid)
        os.chmod(temp_path, stat.S_IMODE(metadata.st_mode))
        # The target is offline; update its shadow database without relying on PAM or its lock setup.
        os.replace(temp_path, shadow_file)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
    print(f"[EXEC] Updated root and {username} passwords in {shadow_file}")


def merge_cache_directory(source: Path, destination: Path):
    if not source.exists():
        return
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        dest_entry = destination / entry.name
        if entry.is_dir() and not entry.is_symlink():
            if dest_entry.is_dir() and not dest_entry.is_symlink():
                merge_cache_directory(entry, dest_entry)
            elif dest_entry.exists() or dest_entry.is_symlink():
                shutil.rmtree(entry)
            else:
                shutil.move(entry, dest_entry)
        elif dest_entry.exists() or dest_entry.is_symlink():
            entry.unlink()
        else:
            shutil.move(entry, dest_entry)
    source.rmdir()


def prepare_target_package_cache(target: Path) -> Path:
    cache_root = target / "var" / "lib" / "libvirt" / "images" / ".multiboot_installer_cache"
    if cache_root.is_symlink():
        sys.exit(f"Error: Package cache path must not be a symlink: {cache_root}")
    cache_root.mkdir(parents=True, exist_ok=True)

    for source_root in (CACHE_DIR, LEGACY_CACHE_DIR):
        if source_root.is_symlink() or not source_root.is_dir():
            continue
        for source in list(source_root.iterdir()):
            if (
                source.is_dir()
                and not source.is_symlink()
                and source.name.startswith(("fedora_", "debian_", "alpine_"))
            ):
                print(f"Moving package cache from {source} to target storage.")
                merge_cache_directory(source, cache_root / source.name)

    alpine_arch = get_arch_spec()["alpine_arch"]
    alpine_cache = cache_root / f"alpine_{alpine_arch}"
    alpine_cache.mkdir(parents=True, exist_ok=True)
    for source_root in (CACHE_DIR, LEGACY_CACHE_DIR, cache_root):
        if source_root.is_symlink() or not source_root.is_dir():
            continue
        for suffix in ("", ".part"):
            source = source_root / f"alpine-minirootfs-{alpine_arch}.tar.gz{suffix}"
            destination = alpine_cache / source.name
            if source == destination or source.is_symlink() or not source.is_file():
                continue
            if destination.is_symlink():
                sys.exit(f"Error: Alpine rootfs cache path must not be a symlink: {destination}")
            if destination.exists() and not destination.is_file():
                sys.exit(f"Error: Expected a file at the Alpine rootfs cache path: {destination}")
            if destination.is_file() and destination.stat().st_size >= source.stat().st_size:
                source.unlink()
                continue
            if destination.exists():
                destination.unlink()
            print(f"Moving cached Alpine rootfs download to target storage: {destination}")
            shutil.move(source, destination)
    return cache_root


def prepare_installer_cache():
    if CACHE_DIR.is_symlink():
        sys.exit(f"Error: Installer cache path must not be a symlink: {CACHE_DIR}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not CACHE_DIR.is_dir() or CACHE_DIR.stat().st_uid != os.geteuid():
        sys.exit(f"Error: Installer cache must be a directory owned by the current user: {CACHE_DIR}")
    CACHE_DIR.chmod(0o700)


def download_cached_file(url: str, destination: Path):
    if destination.is_file() and destination.stat().st_size > 0:
        print(f"[CACHE] Reusing {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.name}.part")
    print(f"--- Downloading {url} ---")
    run(["curl", "-L", "--fail", "--continue-at", "-", "-o", str(partial), url])
    if not partial.is_file() or partial.stat().st_size == 0:
        raise RuntimeError(f"Download did not produce a non-empty cache file: {destination}")
    partial.replace(destination)


def locate_refind_assets() -> tuple[Path | None, Path | None]:
    """Finds refind_<arch>.efi and btrfs_<arch>.efi on the host live system."""
    arch = get_arch_spec()
    sfx = arch["efi_suffix"]
    refind_bin: Path | None = None
    btrfs_drv: Path | None = None

    search_roots = [
        Path("/usr/share/refind"),
        Path("/usr/lib/refind"),
        Path("/boot/efi/EFI/refind"),
    ]
    for root in search_roots:
        for candidate in [root / f"refind_{sfx}.efi", root / "refind" / f"refind_{sfx}.efi"]:
            if candidate.exists():
                refind_bin = candidate
                break
        for drv in [
            root / f"drivers_{sfx}" / f"btrfs_{sfx}.efi",
            root / "refind" / f"drivers_{sfx}" / f"btrfs_{sfx}.efi",
        ]:
            if drv.exists():
                btrfs_drv = drv
                break
        if refind_bin:
            break
    return refind_bin, btrfs_drv


def stage_upstream_refind():
    """Downloads and extracts upstream refind-bin into /usr/share/refind if missing."""
    if REFIND_CACHE_DIR.is_symlink():
        sys.exit(f"Error: rEFInd cache path must not be a symlink: {REFIND_CACHE_DIR}")
    REFIND_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not REFIND_CACHE_DIR.is_dir() or REFIND_CACHE_DIR.stat().st_uid != os.geteuid():
        sys.exit(f"Error: rEFInd cache must be a directory owned by the current user: {REFIND_CACHE_DIR}")
    REFIND_CACHE_DIR.chmod(0o700)
    zip_path = REFIND_CACHE_DIR / f"refind-bin-{REFIND_VERSION}.zip"
    target_partial = zip_path.with_name(f"{zip_path.name}.part")
    if zip_path.is_symlink() or target_partial.is_symlink():
        sys.exit(f"Error: rEFInd cache files must not be symlinks: {zip_path}")
    if zip_path.exists() and not zip_path.is_file():
        sys.exit(f"Error: Expected a file at the rEFInd cache path: {zip_path}")
    if target_partial.exists() and not target_partial.is_file():
        sys.exit(f"Error: Expected a file at the partial rEFInd cache path: {target_partial}")

    cache_sources = (CACHE_DIR, LEGACY_CACHE_DIR)
    for source_root in cache_sources:
        if source_root.is_symlink() or not source_root.is_dir():
            continue
        cached_zip = source_root / zip_path.name
        if cached_zip.is_symlink() or not cached_zip.is_file() or cached_zip.stat().st_size == 0:
            continue
        if not zip_path.is_file() or cached_zip.stat().st_size > zip_path.stat().st_size:
            if zip_path.exists():
                zip_path.unlink()
            if target_partial.exists():
                target_partial.unlink()
            shutil.move(cached_zip, zip_path)
        else:
            cached_zip.unlink()

    if not zip_path.is_file() or zip_path.stat().st_size == 0:
        for source_root in cache_sources:
            if source_root.is_symlink() or not source_root.is_dir():
                continue
            cached_partial = source_root / f"{zip_path.name}.part"
            if cached_partial.is_symlink() or not cached_partial.is_file():
                continue
            if not target_partial.is_file() or cached_partial.stat().st_size > target_partial.stat().st_size:
                if target_partial.exists():
                    target_partial.unlink()
                shutil.move(cached_partial, target_partial)
            else:
                cached_partial.unlink()
    else:
        if target_partial.exists():
            target_partial.unlink()
        for source_root in cache_sources:
            if source_root.is_symlink() or not source_root.is_dir():
                continue
            cached_partial = source_root / f"{zip_path.name}.part"
            if cached_partial.is_file() and not cached_partial.is_symlink():
                cached_partial.unlink()
    extract_dir = REFIND_CACHE_DIR / "refind_extracted"

    download_cached_file(REFIND_ZIP_URL, zip_path)

    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)

    src_root = next(extract_dir.glob("refind-bin-*")) / "refind"
    dest_root = Path("/usr/share/refind")
    if dest_root.exists():
        shutil.rmtree(dest_root)
    shutil.copytree(src_root, dest_root)
    shutil.rmtree(extract_dir)


def preflight_check():
    """Validates host architecture, UEFI state, and required CLI tools before touching any disk."""
    arch = get_arch_spec()
    print(f"\n=== Running Preflight Environment Checks ({arch['family']} / {arch['fedora_arch']}) ===")

    if not Path("/sys/firmware/efi").is_dir():
        sys.exit("Error: System is not booted in UEFI mode (/sys/firmware/efi missing). Reboot in UEFI mode.")

    tool_packages = {
        "parted": ("parted", "parted"),
        "sgdisk": ("gdisk", "gdisk"),
        "wipefs": ("util-linux", "util-linux"),
        "blkid": ("util-linux", "util-linux"),
        "mkfs.fat": ("dosfstools", "dosfstools"),
        "mkfs.ext4": ("e2fsprogs", "e2fsprogs"),
        "mkfs.btrfs": ("btrfs-progs", "btrfs-progs"),
        "btrfs": ("btrfs-progs", "btrfs-progs"),
        "dnf": ("dnf5", "dnf"),
        "debootstrap": ("debootstrap", "debootstrap"),
        "curl": ("curl", "curl"),
        "tar": ("tar", "tar"),
        "openssl": ("openssl", "openssl"),
        "sbsign": ("sbsigntools", "sbsigntool"),
        "sbverify": ("sbsigntools", "sbverify"),
        "mokutil": ("mokutil", "mokutil"),
        "efibootmgr": ("efibootmgr", "efibootmgr"),
    }

    missing_tools = [tool for tool in tool_packages if shutil.which(tool) is None]
    if missing_tools:
        print(f"Missing required host tools: {', '.join(missing_tools)}")
        ans = input("Attempt to install missing packages automatically now? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            if shutil.which("dnf"):
                dnf_cache = CACHE_DIR / "host-dnf"
                dnf_cache.mkdir(parents=True, exist_ok=True)
                pkgs = sorted({tool_packages[t][0] for t in missing_tools})
                run([
                    "dnf", "-y", f"--setopt=cachedir={dnf_cache}", "--setopt=keepcache=True",
                    "install", *pkgs,
                ])
            elif shutil.which("apt-get"):
                apt_cache = CACHE_DIR / "host-apt"
                apt_archives = apt_cache / "archives"
                apt_lists = apt_cache / "lists"
                for apt_dir in (apt_archives / "partial", apt_lists / "partial"):
                    apt_dir.mkdir(parents=True, exist_ok=True)
                apt_opts = [
                    "-o", f"Dir::Cache::archives={apt_archives}",
                    "-o", f"Dir::State::lists={apt_lists}",
                ]
                pkgs = sorted({tool_packages[t][1] for t in missing_tools})
                run(["apt-get", *apt_opts, "update"])
                run(["apt-get", *apt_opts, "install", "-y", *pkgs])
            else:
                sys.exit("No supported package manager (dnf/apt-get) found to auto-install dependencies.")
        else:
            sys.exit("Aborted due to missing host dependencies.")

    still_missing = [tool for tool in tool_packages if shutil.which(tool) is None]
    if still_missing:
        sys.exit(f"Error: The following required tools are still missing: {', '.join(still_missing)}")

    mkswap_check = subprocess.run(
        ["btrfs", "filesystem", "mkswapfile", "--help"], capture_output=True, text=True
    )
    if mkswap_check.returncode != 0:
        sys.exit("Error: Installed btrfs-progs does not support 'btrfs filesystem mkswapfile' (requires >= 6.1).")

    refind_bin, _ = locate_refind_assets()
    if not refind_bin:
        print(f"rEFInd binary (refind_{arch['efi_suffix']}.efi) not found; staging upstream rEFInd.")
        stage_upstream_refind()
        refind_bin, _ = locate_refind_assets()
        if not refind_bin:
            sys.exit(f"Error: refind_{arch['efi_suffix']}.efi is required for UEFI Secure Boot setup.")

    print(f"[OK] Preflight checks passed for {arch['family']} UEFI environment.")


@contextmanager
def chroot_env(
    target: Path,
    extra_binds: list[tuple[Path, Path]] | None = None,
    mount_run: bool = True,
    mount_efivars: bool = True,
):
    binds = ["dev", "dev/pts", "proc", "sys"]
    if mount_run:
        binds.append("run")
    if mount_efivars and Path("/sys/firmware/efi/efivars").is_dir():
        binds.append("sys/firmware/efi/efivars")

    mounted = []
    try:
        for b in binds:
            src = Path("/") / b
            dst = target / b
            dst.mkdir(parents=True, exist_ok=True)
            run(["mount", "--bind", str(src), str(dst)])
            mounted.append(dst)
        for src, dst in extra_binds or []:
            src.mkdir(parents=True, exist_ok=True)
            dst.mkdir(parents=True, exist_ok=True)
            run(["mount", "--bind", str(src), str(dst)])
            mounted.append(dst)
        yield
    finally:
        for dst in reversed(mounted):
            run(["umount", "-l", str(dst)], check=False)


def chroot_run(target: Path, cmd: list, input_text: str | None = None, check: bool = True):
    return run(["chroot", str(target), *map(str, cmd)], check=check, input_text=input_text)


def get_part_path(disk: str, part_num: int) -> str:
    if any(disk.endswith(c) for c in "0123456789"):
        return f"{disk}p{part_num}"
    return f"{disk}{part_num}"


def prompt_user_config(include_mok_note: bool = True) -> dict:
    note = " & MOK Enrollment Password" if include_mok_note else ""
    print(f"\n--- Configure Unified Developer User (UID 1000){note} ---")

    while True:
        username = input("Enter developer username (lowercase, e.g., dev): ").strip()
        if username and username != "root" and username.isalnum() and username.islower():
            break
        print("Invalid username. Use lowercase letters and numbers only; 'root' is reserved.")

    while True:
        pw1 = getpass.getpass(f"Enter password for '{username}' (applied to user & root): ")
        pw2 = getpass.getpass("Confirm password: ")
        if pw1 and pw1 == pw2:
            break
        print("Passwords did not match or were empty. Try again.")

    return {"username": username, "password": pw1, "uid": 1000, "gid": 1000}


def generate_local_mok() -> dict[str, Path]:
    """Loads existing MOK from cache or running OS (/etc/secureboot-mok), or generates a new one."""
    MOK_DIR.mkdir(parents=True, exist_ok=True)
    mok_key = MOK_DIR / "MOK.key"
    mok_crt = MOK_DIR / "MOK.crt"
    mok_der = MOK_DIR / "MOK.der"
    live_mok = Path("/etc/secureboot-mok")

    if not mok_key.exists() and (live_mok / "MOK.key").exists():
        shutil.copy2(live_mok / "MOK.key", mok_key)
        shutil.copy2(live_mok / "MOK.crt", mok_crt)
        shutil.copy2(live_mok / "MOK.der", mok_der)
        mok_key.chmod(0o600)

    if not (mok_key.exists() and mok_crt.exists() and mok_der.exists()):
        print("\n--- Generating Local Machine Owner Key (MOK) for UEFI Secure Boot ---")
        run([
            "openssl", "req", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(mok_key),
            "-new", "-x509", "-sha256", "-days", "3650",
            "-subj", "/CN=Local MultiBoot Developer MOK/",
            "-out", str(mok_crt),
        ])
        run(["openssl", "x509", "-outform", "DER", "-in", str(mok_crt), "-out", str(mok_der)])
        mok_key.chmod(0o600)

    return {"key": mok_key, "crt": mok_crt, "der": mok_der}


def check_and_wipe_disk(disk: str):
    if not Path(disk).is_block_device():
        sys.exit(f"Error: {disk} is not a valid block device.")

    def inspect_disk_usage() -> tuple[set[str], set[Path]]:
        result = subprocess.run(
            ["lsblk", "--json", "--paths", "--output", "PATH,MOUNTPOINTS", disk],
            capture_output=True,
            text=True,
            check=True,
        )
        device_paths: set[str] = set()
        mountpoints: set[Path] = set()

        def collect_devices(device: dict):
            device_paths.add(os.path.realpath(device["path"]))
            for mountpoint in device.get("mountpoints") or []:
                if mountpoint:
                    mountpoints.add(Path(mountpoint).resolve())
            for child in device.get("children") or []:
                collect_devices(child)

        for device in json.loads(result.stdout)["blockdevices"]:
            collect_devices(device)
        return device_paths, mountpoints

    print(f"\n--- Checking existing data on {disk} ---")
    run(["lsblk", "-o", "NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT", disk], check=False)
    device_paths, mountpoints = inspect_disk_usage()
    mount_root = MOUNT_ROOT.resolve()
    mounts_outside_installer = sorted(
        mountpoint for mountpoint in mountpoints if not mountpoint.is_relative_to(mount_root)
    )
    if mounts_outside_installer:
        formatted_mounts = "\n".join(f"  {mountpoint}" for mountpoint in mounts_outside_installer)
        sys.exit(
            f"Error: Refusing to wipe {disk}; its partitions are mounted outside {mount_root}:\n"
            f"{formatted_mounts}\nUnmount them and retry."
        )

    active_swaps = []
    for line in Path("/proc/swaps").read_text().splitlines()[1:]:
        swap_path = line.split()[0]
        resolved_swap = os.path.realpath(swap_path)
        if resolved_swap in device_paths or Path(swap_path).resolve().is_relative_to(mount_root):
            active_swaps.append(swap_path)
    if active_swaps:
        formatted_swaps = "\n".join(f"  {swap_path}" for swap_path in active_swaps)
        sys.exit(
            f"Error: Refusing to wipe {disk}; active swap is using it:\n"
            f"{formatted_swaps}\nDisable the swap with swapoff and retry."
        )

    if mountpoints:
        print("Existing mounts under the installer mount root will be unmounted before wiping:")
        for mountpoint in sorted(mountpoints, key=lambda path: len(path.parts), reverse=True):
            print(f"  {mountpoint}")

    sig = subprocess.run(["wipefs", disk], capture_output=True, text=True)
    if sig.stdout.strip():
        print(f"\nExisting filesystem/partition signatures detected:\n{sig.stdout}")
    confirm = input(f"\nWARNING: All data on {disk} will be DESTROYED. Type 'WIPE {disk}' to continue: ")
    if confirm.strip() != f"WIPE {disk}":
        sys.exit("Aborted by user.")

    for mountpoint in sorted(mountpoints, key=lambda path: len(path.parts), reverse=True):
        run(["umount", str(mountpoint)])

    remaining_devices, remaining_mountpoints = inspect_disk_usage()
    if remaining_mountpoints:
        formatted_mounts = "\n".join(f"  {mountpoint}" for mountpoint in sorted(remaining_mountpoints))
        sys.exit(
            f"Error: Refusing to wipe {disk}; these mounts remain active:\n"
            f"{formatted_mounts}"
        )
    if remaining_devices != device_paths:
        sys.exit(f"Error: The block-device layout for {disk} changed during cleanup; refusing to wipe it.")

    run(["wipefs", "--all", "--force", disk])
    run(["sgdisk", "--zap-all", disk])
    run(["partprobe", disk])


def create_partitions(disk: str) -> tuple[str, str, str]:
    print(f"\n--- Creating GPT & Partitions on {disk} ---")
    run(["parted", "-s", disk, "mklabel", "gpt"])
    run(["parted", "-s", disk, "mkpart", "EFI", "fat32", "1MiB", f"{EFI_PARTITION_END_MIB}MiB"])
    run(["parted", "-s", disk, "set", "1", "esp", "on"])
    run([
        "parted", "-s", disk, "mkpart", "FEDORA_BOOT", "ext4",
        f"{EFI_PARTITION_END_MIB}MiB", f"{BOOT_PARTITION_END_MIB}MiB",
    ])
    run([
        "parted", "-s", disk, "mkpart", "MULTIBOOT_BTRFS", "btrfs",
        f"{BOOT_PARTITION_END_MIB}MiB", "100%",
    ])
    run(["partprobe", disk])

    p_efi = get_part_path(disk, 1)
    p_boot = get_part_path(disk, 2)
    p_btrfs = get_part_path(disk, 3)
    run(["mkfs.fat", "-F32", "-n", "EFI", p_efi])
    run(["mkfs.ext4", "-F", "-L", "FEDORA_BOOT", p_boot])
    run(["mkfs.btrfs", "-f", "-L", "MULTIBOOT_POOL", p_btrfs])
    return p_efi, p_boot, p_btrfs


def select_distros(single_mode: bool = False) -> list[DistroConfig]:
    arch = get_arch_spec()
    print(f"\n--- Select Target Systems ({arch['family']} / {arch['fedora_arch']}) ---")
    for key, info in DISTROS.items():
        print(f"  [{key}] {info['name']} ({info['id']})")

    if single_mode:
        choice = input("\nEnter single system number (e.g., 6): ").strip()
        if choice not in DISTROS:
            sys.exit("Invalid system selection.")
        return [DISTROS[choice]]

    choices = input("\nEnter comma-separated numbers (e.g., 1,3,6): ").strip().split(",")
    selected = [DISTROS[c.strip()] for c in choices if c.strip() in DISTROS]
    if not selected:
        sys.exit("No valid systems selected. Exiting.")
    return selected


def ensure_shared_subvolumes(top_mnt: Path):
    shared_dir = top_mnt / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    for sv in ["@projects", "@containers", "@swap", "@libvirt"]:
        sv_path = shared_dir / sv
        if not sv_path.exists():
            run(["btrfs", "subvolume", "create", str(sv_path)])
    run(["chattr", "+C", str(shared_dir / "@swap")])
    run(["chattr", "+C", str(shared_dir / "@libvirt")])


def create_single_os_subvolumes(top_mnt: Path, distro: DistroConfig):
    os_dir = top_mnt / distro["id"]
    os_dir.mkdir(parents=True, exist_ok=True)
    distro_subvols = ["@", "@home", "@snapshots", "@var_log", "@var_cache", "@var_tmp"]
    dm_config = get_dm_subvolume_config(distro)
    if dm_config:
        distro_subvols.append(dm_config[0])
    for sv in distro_subvols:
        sv_path = os_dir / sv
        if not sv_path.exists():
            run(["btrfs", "subvolume", "create", str(sv_path)])


def wipe_os_folder_subvolumes(top_mnt: Path, os_id: str):
    os_dir = top_mnt / os_id
    if os_dir.exists():
        snap_dir = os_dir / "@snapshots"
        if snap_dir.exists():
            for sid_dir in sorted(snap_dir.iterdir()):
                snap_sv = sid_dir / "snapshot"
                if snap_sv.exists():
                    run(["btrfs", "subvolume", "delete", str(snap_sv)], check=False)
                if sid_dir.is_dir():
                    shutil.rmtree(sid_dir, ignore_errors=True)
        for sub in sorted(os_dir.iterdir()):
            if sub.is_dir():
                run(["btrfs", "subvolume", "delete", str(sub)], check=False)
        os_dir.rmdir()

    swapfile = top_mnt / "shared" / "@swap" / f"swapfile_{os_id}"
    if swapfile.exists():
        swapfile.unlink()


def create_btrfs_subvolumes(p_btrfs: str, selected_distros: list[DistroConfig]):
    top_mnt = MOUNT_ROOT / "btrfs_top"
    top_mnt.mkdir(parents=True, exist_ok=True)
    run(["mount", "-o", "subvolid=5", p_btrfs, str(top_mnt)])
    ensure_shared_subvolumes(top_mnt)
    for d in selected_distros:
        create_single_os_subvolumes(top_mnt, d)
    run(["umount", str(top_mnt)])


def create_distro_swapfile(target_root: Path, distro_id: str, size_gb: int):
    swap_file = target_root / "swap" / f"swapfile_{distro_id}"
    if not swap_file.exists():
        print(f"Creating {size_gb}G Btrfs swapfile for {distro_id}...")
        run(["btrfs", "filesystem", "mkswapfile", "--size", f"{size_gb}g", str(swap_file)])


def prepare_fedora_installroot(target_root: Path):
    etc_dir = target_root / "etc"
    etc_dir.mkdir(parents=True, exist_ok=True)
    passwd_file = etc_dir / "passwd"
    group_file = etc_dir / "group"
    if not passwd_file.exists():
        passwd_file.write_text("root:x:0:0:root:/root:/bin/bash\n")
    if not group_file.exists():
        group_file.write_text("root:x:0:\n")


def prepare_alpine_cache(target_root: Path, cache_dir: Path):
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_link = target_root / "etc" / "apk" / "cache"
    cache_link.parent.mkdir(parents=True, exist_ok=True)
    if cache_link.is_symlink():
        if os.readlink(cache_link) == "/var/cache/apk":
            return
        cache_link.unlink()
    elif cache_link.exists():
        if not cache_link.is_dir():
            sys.exit(f"Error: Expected an APK cache directory or symlink at {cache_link}")
        merge_cache_directory(cache_link, cache_dir)
    os.symlink("/var/cache/apk", cache_link)


def enable_alpine_community_repositories(target_root: Path):
    repositories_file = target_root / "etc" / "apk" / "repositories"
    repositories_file.parent.mkdir(parents=True, exist_ok=True)
    lines = repositories_file.read_text().splitlines() if repositories_file.is_file() else []

    active_main_repositories = []
    commented_community_indexes = []
    has_active_community = False
    has_active_repository = False
    for index, line in enumerate(lines):
        repository = line.strip()
        if not repository:
            continue
        is_commented = repository.startswith("#")
        if is_commented:
            repository = repository[1:].strip()
        if not repository or repository.startswith("#"):
            continue

        normalized = repository.rstrip("/")
        if is_commented:
            if normalized.endswith("/community"):
                commented_community_indexes.append(index)
            continue

        has_active_repository = True
        if normalized.endswith("/community"):
            has_active_community = True
        elif normalized.endswith("/main"):
            active_main_repositories.append(normalized)

    if not has_active_community:
        if commented_community_indexes:
            for index in commented_community_indexes:
                lines[index] = lines[index].strip()[1:].strip()
        elif active_main_repositories:
            lines.extend(
                f"{repository.removesuffix('/main')}/community"
                for repository in active_main_repositories
            )
        else:
            if not has_active_repository:
                lines.append(f"https://dl-cdn.alpinelinux.org/alpine/{ALPINE_BRANCH}/main")
            lines.append(f"https://dl-cdn.alpinelinux.org/alpine/{ALPINE_BRANCH}/community")

    repositories_file.write_text("\n".join(lines).rstrip() + "\n")


def provision_virtio_win_iso(target_root: Path):
    libvirt_images = target_root / "var" / "lib" / "libvirt" / "images"
    libvirt_images.mkdir(parents=True, exist_ok=True)
    target_iso = libvirt_images / "virtio-win.iso"
    target_partial = target_iso.with_name(f"{target_iso.name}.part")
    if target_iso.is_symlink() or target_partial.is_symlink():
        sys.exit(f"Error: VirtIO ISO paths must not be symlinks: {target_iso}")
    if target_partial.exists() and not target_partial.is_file():
        sys.exit(f"Error: Expected a file at the partial VirtIO ISO path: {target_partial}")
    if target_iso.is_file() and target_iso.stat().st_size > 0:
        return
    if target_iso.exists() and not target_iso.is_file():
        sys.exit(f"Error: Expected a file at the VirtIO ISO path: {target_iso}")
    if REFIND_CACHE_DIR.is_symlink():
        sys.exit(f"Error: Persistent installer cache path must not be a symlink: {REFIND_CACHE_DIR}")
    REFIND_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not REFIND_CACHE_DIR.is_dir() or REFIND_CACHE_DIR.stat().st_uid != os.geteuid():
        sys.exit(f"Error: Persistent installer cache must be a directory owned by the current user: {REFIND_CACHE_DIR}")
    REFIND_CACHE_DIR.chmod(0o700)

    cached_iso = REFIND_CACHE_DIR / target_iso.name
    cached_partial = cached_iso.with_name(f"{cached_iso.name}.part")
    if cached_iso.is_symlink() or cached_partial.is_symlink():
        sys.exit(f"Error: Cached VirtIO ISO paths must not be symlinks: {cached_iso}")
    if cached_iso.exists() and not cached_iso.is_file():
        sys.exit(f"Error: Expected a file at the cached VirtIO ISO path: {cached_iso}")
    if cached_partial.exists() and not cached_partial.is_file():
        sys.exit(f"Error: Expected a file at the partial cached VirtIO ISO path: {cached_partial}")

    download_cached_file(VIRTIO_WIN_URL, cached_iso)
    if target_partial.exists():
        target_partial.unlink()
    try:
        print(f"Copying cached VirtIO ISO to target storage: {target_iso}")
        shutil.copy2(cached_iso, target_partial)
        target_partial.replace(target_iso)
    except OSError:
        if target_partial.exists():
            target_partial.unlink()
        raise
    target_iso.chmod(0o644)


def generate_fstab(target_root: Path, p_efi: str, p_boot: str, p_btrfs: str, distro: DistroConfig):
    did = distro["id"]
    uuid_btrfs = subprocess.check_output(["blkid", "-s", "UUID", "-o", "value", p_btrfs], text=True).strip()
    uuid_efi = subprocess.check_output(["blkid", "-s", "UUID", "-o", "value", p_efi], text=True).strip()
    uuid_boot = subprocess.check_output(["blkid", "-s", "UUID", "-o", "value", p_boot], text=True).strip()

    lines = [
        f"# /etc/fstab generated for {distro['name']} (Folder-isolated Btrfs + NVMe KVM layout)",
        f"UUID={uuid_btrfs}  /                        btrfs  subvol={did}/@,{BTRFS_OPTS}  0 0",
        f"UUID={uuid_btrfs}  /home                    btrfs  subvol={did}/@home,{BTRFS_OPTS}  0 0",
        f"UUID={uuid_btrfs}  /.snapshots              btrfs  subvol={did}/@snapshots,{BTRFS_OPTS}  0 0",
        f"UUID={uuid_btrfs}  /var/log                 btrfs  subvol={did}/@var_log,{BTRFS_OPTS}  0 0",
        f"UUID={uuid_btrfs}  /var/cache               btrfs  subvol={did}/@var_cache,{BTRFS_OPTS}  0 0",
        f"UUID={uuid_btrfs}  /var/tmp                 btrfs  subvol={did}/@var_tmp,{BTRFS_OPTS}  0 0",
        f"UUID={uuid_btrfs}  /srv/projects            btrfs  subvol=shared/@projects,{BTRFS_OPTS}  0 0",
        f"UUID={uuid_btrfs}  /var/lib/containers      btrfs  subvol=shared/@containers,{BTRFS_OPTS}  0 0",
        f"UUID={uuid_btrfs}  /var/lib/libvirt/images  btrfs  subvol=shared/@libvirt,{BTRFS_NOCOW_OPTS}  0 0",
        f"UUID={uuid_btrfs}  /swap                    btrfs  subvol=shared/@swap,{BTRFS_NOCOW_OPTS}  0 0",
        f"/swap/swapfile_{did}  none                   swap   defaults,pri=10  0 0",
        f"UUID={uuid_efi}    /boot/efi                vfat   umask=0077  0 2",
    ]
    dm_config = get_dm_subvolume_config(distro)
    if dm_config:
        dm_subvol, dm_mount = dm_config
        lines.append(
            f"UUID={uuid_btrfs}  {dm_mount}  btrfs  subvol={did}/{dm_subvol},{BTRFS_OPTS}  0 0"
        )
    if distro["type"] == "fedora":
        lines.append(f"UUID={uuid_boot}   /boot                ext4   defaults  0 2")

    fstab_path = target_root / "etc" / "fstab"
    fstab_path.parent.mkdir(parents=True, exist_ok=True)
    fstab_path.write_text("\n".join(lines) + "\n")


def configure_snapper_baseline(target: Path, p_btrfs: str, distro: DistroConfig, user_cfg: dict):
    did = distro["id"]
    dtype = distro["type"]
    if dtype == "alpine":
        print(f"Snapper is not available in Alpine {ALPINE_BRANCH} repositories; leaving its snapshots subvolume empty.")
        return

    uname = user_cfg["username"]
    snap_mnt = target / ".snapshots"
    snap_subvol_path = f"{did}/@snapshots"

    print(f"--- Configuring Snapper Baseline & Hooks for {distro['name']} ---")
    run(["umount", str(snap_mnt)])
    if snap_mnt.exists():
        try:
            snap_mnt.rmdir()
        except OSError as exc:
            raise RuntimeError(f"Expected an empty Snapper mountpoint at {snap_mnt}") from exc

    with chroot_env(target):
        chroot_run(target, ["snapper", "--no-dbus", "-c", "root", "create-config", "/"])
        chroot_run(target, ["btrfs", "subvolume", "delete", "/.snapshots"])

    snap_mnt.mkdir(parents=True, exist_ok=True)
    run(["mount", "-o", f"subvol={snap_subvol_path},{BTRFS_OPTS}", p_btrfs, str(snap_mnt)])
    snap_mnt.chmod(0o750)

    snap_cfg = target / "etc" / "snapper" / "configs" / "root"
    if snap_cfg.exists():
        cfg_text = snap_cfg.read_text()
        replacements = {
            'ALLOW_USERS=""': f'ALLOW_USERS="{uname}"',
            'TIMELINE_LIMIT_HOURLY="10"': 'TIMELINE_LIMIT_HOURLY="5"',
            'TIMELINE_LIMIT_DAILY="10"': 'TIMELINE_LIMIT_DAILY="7"',
            'TIMELINE_LIMIT_WEEKLY="0"': 'TIMELINE_LIMIT_WEEKLY="2"',
            'TIMELINE_LIMIT_MONTHLY="10"': 'TIMELINE_LIMIT_MONTHLY="1"',
            'TIMELINE_LIMIT_YEARLY="10"': 'TIMELINE_LIMIT_YEARLY="0"',
        }
        for old, new in replacements.items():
            cfg_text = cfg_text.replace(old, new)
        snap_cfg.write_text(cfg_text)

    with chroot_env(target):
        if dtype == "fedora":
            actions_dir = target / "etc" / "dnf" / "libdnf5-plugins" / "actions.d"
            actions_dir.mkdir(parents=True, exist_ok=True)
            (actions_dir / "snapper.actions").write_text(
                "# Automatic Snapper pre/post snapshots for DNF5\n"
                "pre_transaction::::/usr/bin/snapper create --type=pre --cleanup-algorithm=number "
                "--print-number --userdata=important=yes --description=dnf5_${{pid}}\n"
                "post_transaction::::/usr/bin/snapper create --type=post --cleanup-algorithm=number "
                "--pre-number=${{pre_snap_id}} --userdata=important=yes --description=dnf5_${{pid}}\n"
            )
            chroot_run(target, ["systemctl", "enable", "snapper-timeline.timer", "snapper-cleanup.timer"], check=False)
        elif dtype == "debian":
            chroot_run(target, ["systemctl", "enable", "snapper-timeline.timer", "snapper-cleanup.timer"], check=False)

        chroot_run(
            target,
            [
                "snapper", "--no-dbus", "-c", "root", "create",
                "--type", "single",
                "--cleanup-algorithm", "number",
                "--description", f"Baseline clean install ({distro['name']})",
            ],
        )


def sign_alpine_kernel(kernel: Path, key: Path, cert: Path):
    signed_kernel = kernel.with_name(f"{kernel.name}.signed")
    try:
        run([
            "sbsign",
            "--key", str(key),
            "--cert", str(cert),
            "--output", str(signed_kernel),
            str(kernel),
        ])
        verification = subprocess.run(
            ["sbverify", "--cert", str(cert), str(signed_kernel)],
            capture_output=True,
            text=True,
        )
        if verification.returncode != 0:
            detail = verification.stderr.strip() or verification.stdout.strip()
            raise RuntimeError(f"Alpine kernel signature verification failed for {kernel}: {detail}")
        shutil.copystat(kernel, signed_kernel)
        signed_kernel.replace(kernel)
    finally:
        signed_kernel.unlink(missing_ok=True)


def write_alpine_kernel_hook(target: Path):
    hook_script = target / "etc" / "kernel-hooks.d" / "50-sign-kernel-mok.hook"
    hook_script.parent.mkdir(parents=True, exist_ok=True)
    hook_script.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        '[ "$#" -ge 2 ] && [ "$1" = "lts" ] && [ -n "$2" ] || exit 0\n'
        'kernel="/boot/vmlinuz-lts"\n'
        '[ -f "$kernel" ] || exit 0\n'
        'if sbverify --cert /etc/secureboot-mok/MOK.crt "$kernel" >/dev/null 2>&1; then exit 0; fi\n'
        'tmp="$kernel.signed.$$"\n'
        'trap \'rm -f "$tmp"\' EXIT HUP INT TERM\n'
        'sbsign --key /etc/secureboot-mok/MOK.key --cert /etc/secureboot-mok/MOK.crt '
        '--output "$tmp" "$kernel"\n'
        'mv -f "$tmp" "$kernel"\n'
    )
    hook_script.chmod(0o755)


def configure_secure_boot_module_signing(target: Path, distro: DistroConfig, mok: dict[str, Path]):
    dtype = distro["type"]
    print(f"--- Configuring Secure Boot Module & Kernel Signing for {distro['name']} ---")
    sys_mok_dir = target / "etc" / "secureboot-mok"
    sys_mok_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(mok["key"], sys_mok_dir / "MOK.key")
    shutil.copy2(mok["crt"], sys_mok_dir / "MOK.crt")
    shutil.copy2(mok["der"], sys_mok_dir / "MOK.der")
    (sys_mok_dir / "MOK.key").chmod(0o600)

    if dtype == "fedora":
        akmods_priv = target / "etc" / "pki" / "akmods" / "private"
        akmods_cert = target / "etc" / "pki" / "akmods" / "certs"
        akmods_priv.mkdir(parents=True, exist_ok=True)
        akmods_cert.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mok["key"], akmods_priv / "private_key.priv")
        shutil.copy2(mok["der"], akmods_cert / "public_key.der")
        (akmods_priv / "private_key.priv").chmod(0o600)
        for vmlinuz in sorted((target / "boot").glob("vmlinuz-*")):
            tmp_output = vmlinuz.with_suffix(vmlinuz.suffix + ".signed")
            run([
                "sbsign",
                "--key", str(mok["key"]),
                "--cert", str(mok["crt"]),
                "--output", str(tmp_output),
                str(vmlinuz),
            ])
            shutil.move(tmp_output, vmlinuz)
        install_dir = target / "etc" / "kernel" / "install.d"
        install_dir.mkdir(parents=True, exist_ok=True)
        hook = install_dir / "50-multiboot-sbsign.install"
        hook.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "add" ] && [ -n "$2" ] && [ -f "$3" ]; then\n'
            '  sbsign --key /etc/secureboot-mok/MOK.key --cert /etc/secureboot-mok/MOK.crt '
            '--output "$3.signed" "$3" && mv -f "$3.signed" "$3"\n'
            "fi\n"
        )
        hook.chmod(0o755)
    elif dtype == "debian":
        dkms_mok_dir = target / "var" / "lib" / "shim-signed" / "mok"
        dkms_mok_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mok["key"], dkms_mok_dir / "MOK.priv")
        shutil.copy2(mok["der"], dkms_mok_dir / "MOK.der")
        (dkms_mok_dir / "MOK.priv").chmod(0o600)
        dkms_conf = target / "etc" / "dkms" / "framework.conf.d" / "secureboot.conf"
        dkms_conf.parent.mkdir(parents=True, exist_ok=True)
        dkms_conf.write_text(
            'mok_signing_key="/var/lib/shim-signed/mok/MOK.priv"\n'
            'mok_certificate="/var/lib/shim-signed/mok/MOK.der"\n'
        )
        for vmlinuz in sorted((target / "boot").glob("vmlinuz-*")):
            tmp_output = vmlinuz.with_suffix(vmlinuz.suffix + ".signed")
            run([
                "sbsign",
                "--key", str(mok["key"]),
                "--cert", str(mok["crt"]),
                "--output", str(tmp_output),
                str(vmlinuz),
            ])
            shutil.move(tmp_output, vmlinuz)
        hook_dir = target / "etc" / "kernel" / "postinst.d"
        hook_dir.mkdir(parents=True, exist_ok=True)
        hook = hook_dir / "zz-multiboot-sbsign"
        hook.write_text(
            "#!/bin/sh\n"
            'set -e\n'
            'version="$1"\n'
            '[ -n "$version" ] || exit 0\n'
            'kernel="/boot/vmlinuz-$version"\n'
            '[ -f "$kernel" ] || exit 0\n'
            'tmp="$kernel.signed"\n'
            'sbsign --key /etc/secureboot-mok/MOK.key --cert /etc/secureboot-mok/MOK.crt '
            '--output "$tmp" "$kernel"\n'
            'mv -f "$tmp" "$kernel"\n'
        )
        hook.chmod(0o755)
    elif dtype == "alpine":
        vmlinuz = target / "boot" / "vmlinuz-lts"
        if vmlinuz.exists():
            sign_alpine_kernel(vmlinuz, mok["key"], mok["crt"])
        write_alpine_kernel_hook(target)


def configure_post_chroot(
    target: Path,
    p_btrfs: str,
    distro: DistroConfig,
    user_cfg: dict,
    mok: dict[str, Path],
):
    did = distro["id"]
    dtype = distro["type"]
    uname = user_cfg["username"]
    passwd = user_cfg["password"]
    uid = str(user_cfg["uid"])
    gid = str(user_cfg["gid"])
    hostname = f"dev-{did.replace('_', '-')}"
    print(f"\n--- Configuring Post-Chroot User, KVM/Libvirt & Services for {distro['name']} ---")
    (target / "etc" / "hostname").write_text(f"{hostname}\n")
    (target / "etc" / "timezone").write_text("UTC\n")
    localtime = target / "etc" / "localtime"
    if localtime.is_symlink() or localtime.is_file():
        localtime.unlink()
    os.symlink("/usr/share/zoneinfo/UTC", localtime)
    if dtype == "alpine":
        alpine_clock_dir = target / "etc" / "conf.d"
        alpine_clock_dir.mkdir(parents=True, exist_ok=True)
        (alpine_clock_dir / "clock").write_text('clock="UTC"\n')

    extra_binds: list[tuple[Path, Path]] = []
    if dtype == "alpine":
        alpine_apk_cache = (
            target / "var" / "lib" / "libvirt" / "images"
            / ".multiboot_installer_cache" / f"alpine_{get_arch_spec()['alpine_arch']}" / "apk"
        )
        alpine_apk_cache.mkdir(parents=True, exist_ok=True)
        extra_binds.append((alpine_apk_cache, target / "var" / "cache" / "apk"))

    with chroot_env(target, extra_binds):
        if dtype in ("fedora", "debian"):
            admin_group = "wheel" if dtype == "fedora" else "sudo"
            chroot_run(target, ["groupadd", "-f", "-g", gid, uname])
            chroot_run(target, ["groupadd", "-f", "libvirt"], check=False)
            chroot_run(target, ["groupadd", "-f", "kvm"], check=False)
            chroot_run(
                target,
                ["useradd", "-m", "-u", uid, "-g", gid, "-G", f"{admin_group},libvirt,kvm", "-s", "/bin/bash", uname],
            )
            set_target_passwords(target, uname, passwd)
            sudoers_dropin = target / "etc" / "sudoers.d" / f"10-{admin_group}"
            sudoers_dropin.parent.mkdir(parents=True, exist_ok=True)
            sudoers_dropin.write_text(f"%{admin_group} ALL=(ALL:ALL) ALL\n")
            sudoers_dropin.chmod(0o440)
            # Debian's gdm3 package selects display-manager.service via debconf; its unit is static.
            if distro["dm"] and not (dtype == "debian" and distro["dm"] == "gdm3"):
                chroot_run(target, ["systemctl", "enable", distro["dm"]], check=False)
            chroot_run(target, ["systemctl", "enable", "libvirtd"], check=False)
            if dtype == "fedora":
                (target / ".autorelabel").touch()

        elif dtype == "alpine":
            chroot_run(
                target,
                [
                    "/sbin/apk", "add",
                    "shadow", "sudo", "doas", "bash", "sbsigntool",
                ],
            )
            network_interfaces = target / "etc" / "network" / "interfaces"
            network_interfaces.parent.mkdir(parents=True, exist_ok=True)
            network_interfaces.write_text(
                "auto lo\n"
                "iface lo inet loopback\n"
                "\n"
                "auto eth0\n"
                "iface eth0 inet dhcp\n"
            )
            chroot_run(target, ["rc-update", "add", "networking", "boot"])
            chroot_run(target, ["setup-keymap", "us", "us"], check=False)
            keymaps_conf = target / "etc" / "conf.d" / "keymaps"
            keymaps_conf.parent.mkdir(parents=True, exist_ok=True)
            keymaps_conf.write_text('keymap="us"\n')
            chroot_run(target, ["rc-update", "add", "keymaps", "boot"], check=False)
            chroot_run(target, ["rc-update", "add", "udev", "boot"], check=False)
            chroot_run(target, ["rc-update", "add", "udev-trigger", "boot"], check=False)
            chroot_run(target, ["rc-update", "add", "seatd", "default"], check=False)
            chroot_run(target, ["groupadd", "-f", "-g", gid, uname])
            chroot_run(target, ["groupadd", "-f", "libvirt"], check=False)
            chroot_run(target, ["groupadd", "-f", "kvm"], check=False)
            chroot_run(
                target,
                [
                    "useradd", "-m", "-u", uid, "-g", gid,
                    "-G", "wheel,video,input,audio,libvirt,kvm", "-s", "/bin/bash", uname,
                ],
            )
            set_target_passwords(target, uname, passwd)
            sudoers_dropin = target / "etc" / "sudoers.d" / "10-wheel"
            sudoers_dropin.parent.mkdir(parents=True, exist_ok=True)
            sudoers_dropin.write_text("%wheel ALL=(ALL:ALL) ALL\n")
            sudoers_dropin.chmod(0o440)
            doas_conf = target / "etc" / "doas.d" / "doas.conf"
            doas_conf.parent.mkdir(parents=True, exist_ok=True)
            doas_conf.write_text("permit persist :wheel\n")
            modules_file = target / "etc" / "modules"
            existing_mods = modules_file.read_text() if modules_file.exists() else ""
            module_names = [
                "tun",
                "i8042",
                "serio",
                "serio_raw",
                "usbhid",
                "hid_generic",
                "hv_vmbus",
                "hid_hyperv",
                "hv_balloon",
                "hv_utils",
                "hv_netvsc",
                "hv_storvsc",
            ]
            module_lines = existing_mods.splitlines()
            for module_name in module_names:
                if module_name not in module_lines:
                    module_lines.append(module_name)
            modules_file.write_text("\n".join(module_lines) + "\n")

            modules_load_dir = target / "etc" / "modules-load.d"
            modules_load_dir.mkdir(parents=True, exist_ok=True)
            (modules_load_dir / "hyperv-keyboard.conf").write_text(
                "i8042\nserio\nserio_raw\nusbhid\nhid_generic\nhv_vmbus\nhid_hyperv\n"
            )

            xorg_dir = target / "etc" / "X11" / "xorg.conf.d"
            xorg_dir.mkdir(parents=True, exist_ok=True)
            (xorg_dir / "00-hyperv-keyboard.conf").write_text(
                'Section "InputClass"\n'
                '    Identifier "Hyper-V keyboard"\n'
                '    MatchIsKeyboard "on"\n'
                '    Option "XkbLayout" "us"\n'
                "EndSection\n"
            )
            sway_dir = target / "etc" / "sway" / "config.d"
            sway_dir.mkdir(parents=True, exist_ok=True)
            (sway_dir / "10-hyperv-keyboard.conf").write_text(
                "input type:keyboard {\n"
                "    xkb_layout us\n"
                "}\n"
            )

            inittab = target / "etc" / "inittab"
            if inittab.exists():
                inittab_text = inittab.read_text()
                gettys = [
                    "tty1::respawn:/sbin/getty 38400 tty1",
                    "ttyS0::respawn:/sbin/getty -L 115200 ttyS0 vt100",
                    "hvc0::respawn:/sbin/getty -L 115200 hvc0 vt100",
                ]
                for getty in gettys:
                    console_name = getty.split(":", 1)[0]
                    if not any(
                        line.strip().startswith(f"{console_name}:")
                        for line in inittab_text.splitlines()
                    ):
                        inittab_text = inittab_text.rstrip() + "\n" + getty + "\n"
                inittab.write_text(inittab_text)
            if distro["dm"]:
                if distro["dm"] == "lightdm":
                    chroot_run(target, ["rc-update", "add", "dbus", "default"])
                    chroot_run(target, ["rc-update", "add", "elogind", "default"])
                chroot_run(target, ["rc-update", "add", distro["dm"], "default"], check=False)
                chroot_run(target, ["rc-update", "add", "libvirtd", "default"], check=False)
            if did == "alpine_sway":
                chroot_run(target, ["usermod", "-aG", "seat", uname], check=False)
                chroot_run(target, ["rc-update", "add", "seatd", "default"], check=False)

    projects_dir = target / "srv" / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    os.chown(projects_dir, int(uid), int(gid))
    libvirt_dir = target / "var" / "lib" / "libvirt" / "images"
    libvirt_dir.mkdir(parents=True, exist_ok=True)
    libvirt_dir.chmod(0o775)
    user_proj_link = target / "home" / uname / "Projects"
    if not user_proj_link.exists():
        os.symlink("/srv/projects", user_proj_link)
    os.lchown(user_proj_link, int(uid), int(gid))

    configure_snapper_baseline(target, p_btrfs, distro, user_cfg)
    configure_secure_boot_module_signing(target, distro, mok)


def apply_debian_overlay(target: Path):
    overlay = Path(__file__).resolve().parent / "overlay" / "debian"
    if not overlay.is_dir():
        raise FileNotFoundError(f"Debian configuration overlay not found: {overlay}")

    shutil.copytree(overlay, target, dirs_exist_ok=True)
    legacy_sources = target / "etc" / "apt" / "sources.list"
    if legacy_sources.exists() or legacy_sources.is_symlink():
        legacy_sources.unlink()


def configure_flathub(target: Path):
    with chroot_env(target, mount_run=False, mount_efivars=False):
        chroot_run(
            target,
            [
                FLATPAK_PACKAGE,
                "remote-add",
                "--system",
                "--if-not-exists",
                FLATHUB_REMOTE_NAME,
                FLATHUB_REMOTE_URL,
            ],
        )


def configure_snapd(target: Path):
    with chroot_env(target, mount_run=False, mount_efivars=False):
        chroot_run(target, ["systemctl", "enable", SNAPD_SOCKET])

    snap_link = target / "snap"
    if snap_link.is_symlink():
        if os.readlink(snap_link) != SNAP_CLASSIC_PATH:
            raise RuntimeError(f"Unexpected Snap classic path link: {snap_link}")
    elif snap_link.is_dir() and not any(snap_link.iterdir()):
        snap_link.rmdir()
        snap_link.symlink_to(SNAP_CLASSIC_PATH)
    elif snap_link.exists():
        raise FileExistsError(f"Cannot create Snap classic path; {snap_link} already exists.")
    else:
        snap_link.symlink_to(SNAP_CLASSIC_PATH)


def bootstrap_distro(
    p_efi: str,
    p_boot: str,
    p_btrfs: str,
    distro: DistroConfig,
    user_cfg: dict,
    mok: dict[str, Path],
) -> dict:
    arch = get_arch_spec()
    sfx = arch["efi_suffix"]
    did = distro["id"]
    target = MOUNT_ROOT / did
    target.mkdir(parents=True, exist_ok=True)
    run(["mount", "-o", f"subvol={did}/@,{BTRFS_OPTS}", p_btrfs, str(target)])

    for mnt, sv_path, opts in [
        ("home", f"{did}/@home", BTRFS_OPTS),
        (".snapshots", f"{did}/@snapshots", BTRFS_OPTS),
        ("var/log", f"{did}/@var_log", BTRFS_OPTS),
        ("var/cache", f"{did}/@var_cache", BTRFS_OPTS),
        ("var/tmp", f"{did}/@var_tmp", BTRFS_OPTS),
        ("srv/projects", "shared/@projects", BTRFS_OPTS),
        ("var/lib/containers", "shared/@containers", BTRFS_OPTS),
        ("var/lib/libvirt/images", "shared/@libvirt", BTRFS_NOCOW_OPTS),
        ("swap", "shared/@swap", BTRFS_NOCOW_OPTS),
    ]:
        (target / mnt).mkdir(parents=True, exist_ok=True)
        run(["mount", "-o", f"subvol={sv_path},{opts}", p_btrfs, str(target / mnt)])

    package_cache_root = prepare_target_package_cache(target)
    dm_config = get_dm_subvolume_config(distro)
    if dm_config:
        dm_subvol, dm_mount = dm_config
        dm_path = target / dm_mount.lstrip("/")
        dm_path.mkdir(parents=True, exist_ok=True)
        run(["mount", "-o", f"subvol={did}/{dm_subvol},{BTRFS_OPTS}", p_btrfs, str(dm_path)])

    if distro["type"] == "fedora":
        (target / "boot").mkdir(parents=True, exist_ok=True)
        run(["mount", p_boot, str(target / "boot")])
        (target / "boot" / "efi").mkdir(parents=True, exist_ok=True)
        run(["mount", p_efi, str(target / "boot" / "efi")])

    create_distro_swapfile(target, did, distro["swap_gb"])
    provision_virtio_win_iso(target)
    print(f"\n=== Bootstrapping {distro['name']} ({arch['family']} + KVM/UEFI/swtpm) ===")

    if distro["type"] == "fedora":
        prepare_fedora_installroot(target)
        fedora_cache = package_cache_root / f"fedora_{arch['fedora_arch']}"
        fedora_cache.mkdir(parents=True, exist_ok=True)
        cache_arg = ["--setopt=keepcache=True", f"--setopt=cachedir={fedora_cache}"]
        rpmfusion_release_packages = get_rpmfusion_release_packages()
        cmd = [
            "dnf", "-y", "--use-host-config", f"--installroot={target}", f"--forcearch={arch['fedora_arch']}",
            "install", "@core", "@virtualization", arch["fedora_efi_pkg"], "swtpm", "swtpm-tools",
            FLATPAK_PACKAGE, SNAPD_PACKAGE, "kernel", "btrfs-progs", "snapper", "libdnf5-plugin-actions",
            "sudo", "akmods", "kmodtool", "openssl", "mokutil",
            f"shim-{sfx}", f"grub2-efi-{sfx}", distro["desktop"],
        ] + distro.get("initial_packages", []) + rpmfusion_release_packages + cache_arg
        with chroot_env(target, mount_run=False, mount_efivars=False):
            run(cmd)
    elif distro["type"] == "debian":
        deb_cache = package_cache_root / f"debian_{arch['debian_arch']}"
        deb_cache.mkdir(parents=True, exist_ok=True)
        run([
            "debootstrap", f"--arch={arch['debian_arch']}", f"--cache-dir={deb_cache}",
            "trixie", str(target), "https://deb.debian.org/debian",
        ])
        apply_debian_overlay(target)
        apt_cache = deb_cache / "apt"
        apt_archives = apt_cache / "archives"
        apt_lists = apt_cache / "lists"
        for apt_dir in (apt_archives / "partial", apt_lists / "partial"):
            apt_dir.mkdir(parents=True, exist_ok=True)
        with chroot_env(
            target,
            [
                (apt_archives, target / "var" / "cache" / "apt" / "archives"),
                (apt_lists, target / "var" / "lib" / "apt" / "lists"),
            ],
        ):
            chroot_run(target, ["apt-get", "update"])
            chroot_run(
                target,
                [
                    "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
                    f"linux-image-{arch['debian_arch']}", "btrfs-progs", "snapper", SNAPD_PACKAGE, "sudo",
                    arch["debian_qemu_pkg"], "libvirt-daemon-system", "libvirt-clients",
                    arch["debian_efi_pkg"], "swtpm", "swtpm-tools", "virt-manager", FLATPAK_PACKAGE,
                    "dkms", f"linux-headers-{arch['debian_arch']}", "shim-signed",
                    f"grub-efi-{arch['debian_arch']}-signed",
                    "mokutil", "sbsigntool", distro["desktop"],
                    *distro.get("initial_packages", []),
                ],
            )
    elif distro["type"] == "alpine":
        a_arch = arch["alpine_arch"]
        alpine_cache = package_cache_root / f"alpine_{a_arch}"
        alpine_tar = alpine_cache / f"alpine-minirootfs-{a_arch}.tar.gz"
        url = (
            f"https://dl-cdn.alpinelinux.org/alpine/{ALPINE_BRANCH}/releases/{a_arch}/"
            f"alpine-minirootfs-{ALPINE_VERSION}-{a_arch}.tar.gz"
        )
        download_cached_file(url, alpine_tar)
        run(["tar", "-xzf", str(alpine_tar), "-C", str(target)])
        shutil.copy("/etc/resolv.conf", target / "etc" / "resolv.conf")
        enable_alpine_community_repositories(target)
        alpine_apk_cache = package_cache_root / f"alpine_{a_arch}" / "apk"
        prepare_alpine_cache(target, alpine_apk_cache)
        with chroot_env(target, [(alpine_apk_cache, target / "var" / "cache" / "apk")]):
            chroot_run(
                target,
                [
                    "env", "TMPDIR=/var/tmp", "/sbin/apk", "add",
                    "alpine-base", "linux-lts", "kernel-hooks", "btrfs-progs", "openrc",
                    arch["qemu_system"], "qemu-img", "libvirt-daemon", "libvirt-client",
                    arch["alpine_efi_pkg"], "swtpm", "virt-install",
                    "kbd", "eudev", "libinput", "xf86-input-libinput", FLATPAK_PACKAGE,
                    *distro.get("initial_packages", []),
                    *distro["desktop"].split(),
                ],
            )
            mkinitfs_conf = target / "etc" / "mkinitfs" / "mkinitfs.conf"
            if mkinitfs_conf.is_file():
                conf_text = mkinitfs_conf.read_text()
                if "features=" in conf_text:
                    lines = conf_text.splitlines()
                    for index, line in enumerate(lines):
                        if line.startswith("features=") and "btrfs" not in line:
                            lines[index] = line.rstrip()[:-1] + " btrfs\""
                            break
                    mkinitfs_conf.write_text("\n".join(lines) + "\n")
                elif "btrfs" not in conf_text:
                    mkinitfs_conf.write_text(conf_text.rstrip() + '\nfeatures="btrfs"\n')
            chroot_run(
                target,
                [
                    "sh", "-c",
                    'set -e; kver="$(ls -1 /lib/modules 2>/dev/null | sort -V | tail -n 1)"; '
                    'test -n "$kver"; mkinitfs -o /boot/initramfs-lts "$kver"',
                ],
            )

    if distro["type"] in ("fedora", "debian"):
        configure_snapd(target)
    configure_flathub(target)
    generate_fstab(target, p_efi, p_boot, p_btrfs, distro)
    configure_post_chroot(target, p_btrfs, distro, user_cfg, mok)

    boot_dir = target / "boot"
    kernel_file = ""
    initrd_file = ""
    if distro["type"] == "fedora":
        kernels = sorted(boot_dir.glob("vmlinuz-*"))
        initrds = sorted(boot_dir.glob("initramfs-*.img"))
        if kernels and initrds:
            kernel = kernels[-1]
            kernel_suffix = kernel.name.removeprefix("vmlinuz-")
            initrd = next(
                (item for item in reversed(initrds) if item.name == f"initramfs-{kernel_suffix}.img"),
                None,
            )
            if initrd:
                kernel_file = f"/{kernel.name}"
                initrd_file = f"/{initrd.name}"
    elif distro["type"] == "debian":
        kernels = sorted(boot_dir.glob("vmlinuz-*"))
        initrds = sorted(boot_dir.glob("initrd.img-*"))
        if kernels:
            kernel_file = f"/{did}/@/boot/{kernels[-1].name}"
        if initrds:
            initrd_file = f"/{did}/@/boot/{initrds[-1].name}"
    elif distro["type"] == "alpine":
        kernels = sorted(boot_dir.glob("vmlinuz-lts*"))
        initrds = sorted(boot_dir.glob("initramfs-lts*"))
        if kernels:
            kernel_file = f"/{did}/@/boot/{kernels[-1].name}"
        if initrds:
            initrd_file = f"/{did}/@/boot/{initrds[-1].name}"

    run(["umount", "-R", str(target)])
    return {
        "distro": distro,
        "kernel": kernel_file,
        "initrd": initrd_file,
    }


def copy_efi_file(source: Path, destination: Path):
    """Copy a file to the EFI partition with a clear capacity error."""
    source_size = source.stat().st_size
    filesystem = os.statvfs(destination.parent)
    available = filesystem.f_bavail * filesystem.f_frsize
    if source_size > available:
        available_mib = available // (1024 * 1024)
        sys.exit(
            f"Error: EFI partition ran out of space copying {source.name}; "
            f"{source_size // (1024 * 1024)} MiB is needed, but only {available_mib} MiB is available."
        )
    try:
        shutil.copy2(source, destination)
    except OSError as exc:
        if destination.is_file():
            destination.unlink()
        if exc.errno == errno.ENOSPC:
            sys.exit(
                f"Error: EFI partition ran out of space copying {source.name}. "
                "The EFI partition may need to be larger or unused EFI files may need cleanup."
            )
        raise


def setup_refind_and_shim(
    disk: str,
    p_efi: str,
    p_boot: str,
    p_btrfs: str,
    boot_entries: list[dict],
    mok: dict[str, Path],
    user_cfg: dict,
):
    arch = get_arch_spec()
    sfx = arch["efi_suffix"]
    print(f"\n=== Installing Official {arch['family']} Shim + MOK-Signed rEFInd Bootloader ===")
    efi_mnt = MOUNT_ROOT / "efi_setup"
    efi_mnt.mkdir(parents=True, exist_ok=True)
    run(["mount", p_efi, str(efi_mnt)])
    uuid_btrfs = subprocess.check_output(["blkid", "-s", "UUID", "-o", "value", p_btrfs], text=True).strip()
    partuuid_btrfs = subprocess.check_output(
        ["blkid", "-s", "PARTUUID", "-o", "value", p_btrfs], text=True
    ).strip()
    partuuid_boot = subprocess.check_output(
        ["blkid", "-s", "PARTUUID", "-o", "value", p_boot], text=True
    ).strip()
    shim_name = f"shim{sfx}.efi"
    mm_name = f"mm{sfx}.efi"
    grub_name = f"grub{sfx}.efi"
    boot_fallback_name = f"boot{sfx}.efi"
    shim_src: Path | None = None
    mm_src: Path | None = None

    for candidate_dir in [
        efi_mnt / "EFI" / "fedora",
        efi_mnt / "EFI" / "debian",
        efi_mnt / "EFI" / "ubuntu",
        Path("/boot/efi/EFI/fedora"),
        Path("/usr/share/shim"),
    ]:
        if (candidate_dir / shim_name).exists():
            shim_src = candidate_dir / shim_name
            mm_src = candidate_dir / mm_name
            break

    if not shim_src:
        sys.exit(f"Error: Official Microsoft-CA signed {shim_name} not found on EFI partition.")

    refind_dir = efi_mnt / "EFI" / "refind"
    boot_fallback_dir = efi_mnt / "EFI" / "BOOT"
    refind_dir.mkdir(parents=True, exist_ok=True)
    boot_fallback_dir.mkdir(parents=True, exist_ok=True)

    fedora_efi_dir = refind_dir / "fedora"
    fedora_efi_dir.mkdir(parents=True, exist_ok=True)
    boot_mnt = MOUNT_ROOT / "boot_setup"
    boot_mnt.mkdir(parents=True, exist_ok=True)
    run(["mount", p_boot, str(boot_mnt)])
    fedora_files: set[Path] = set()
    for entry in boot_entries:
        if entry["distro"]["type"] != "fedora":
            continue
        for image in (entry["kernel"], entry["initrd"]):
            if image:
                source = boot_mnt / Path(image).name
                if source.is_file():
                    fedora_files.add(source)
    for source in sorted(fedora_files):
        copy_efi_file(source, fedora_efi_dir / source.name)

    refind_bin, btrfs_drv_src = locate_refind_assets()
    if not refind_bin:
        sys.exit(f"Error: refind_{sfx}.efi binary not found.")
    src_refind_dir = refind_bin.parent
    if (src_refind_dir / "icons").is_dir() and not (refind_dir / "icons").exists():
        shutil.copytree(src_refind_dir / "icons", refind_dir / "icons")

    drivers_dst = refind_dir / f"drivers_{sfx}"
    drivers_dst.mkdir(parents=True, exist_ok=True)
    if btrfs_drv_src and btrfs_drv_src.exists():
        copy_efi_file(btrfs_drv_src, drivers_dst / f"btrfs_{sfx}.efi")

    signed_refind = refind_dir / grub_name
    run([
        "sbsign",
        "--key", str(mok["key"]),
        "--cert", str(mok["crt"]),
        "--output", str(signed_refind),
        str(refind_bin),
    ])
    copy_efi_file(shim_src, refind_dir / shim_name)
    copy_efi_file(shim_src, boot_fallback_dir / boot_fallback_name)
    copy_efi_file(signed_refind, boot_fallback_dir / grub_name)
    if mm_src and mm_src.exists():
        copy_efi_file(mm_src, refind_dir / mm_name)
        copy_efi_file(mm_src, boot_fallback_dir / mm_name)
    copy_efi_file(mok["der"], efi_mnt / "MOK.der")

    btrfs_drv = drivers_dst / f"btrfs_{sfx}.efi"
    if btrfs_drv.exists():
        run([
            "sbsign",
            "--key", str(mok["key"]),
            "--cert", str(mok["crt"]),
            "--output", str(btrfs_drv),
            str(btrfs_drv),
        ])

    stanzas = [
        f"# Generated by Multi-Distro {arch['family']} Secure Boot Installer",
        "timeout 10",
        "use_nvram false",
        "scanfor manual,external",
        "",
    ]

    for entry in boot_entries:
        d = entry["distro"]
        did = d["id"]
        dtype = d["type"]
        kernel = entry["kernel"]
        initrd = entry["initrd"]
        if not kernel or not initrd:
            print(f"Warning: No kernel/initrd pair found for {d['name']}; skipping rEFInd stanza.")
            continue
        if dtype == "fedora":
            volume_id = partuuid_efi = subprocess.check_output(
                ["blkid", "-s", "PARTUUID", "-o", "value", p_efi], text=True
            ).strip()
            options = (
                f"root=UUID={uuid_btrfs} rootflags=subvol={did}/@ "
                f"nvme_core.default_ps_max_latency_us=0 rw quiet rhgb"
            )
            kernel_name = Path(kernel).name
            initrd_name = Path(initrd).name
            kernel = f"/EFI/refind/fedora/{kernel_name}"
            initrd = f"/EFI/refind/fedora/{initrd_name}"
        elif dtype == "debian":
            volume_id = partuuid_btrfs
            options = (
                f"root=UUID={uuid_btrfs} rootflags=subvol={did}/@ "
                f"rootfstype=btrfs nvme_core.default_ps_max_latency_us=0 rw quiet"
            )
        else:
            volume_id = partuuid_btrfs
            options = (
                f"root=UUID={uuid_btrfs} rootflags=subvol={did}/@ "
                f"modules=sd-mod,usb-storage,btrfs rootfstype=btrfs "
                f"console=tty1 console=ttyS0,115200n8 rw"
            )
        stanzas.extend([
            f'menuentry "{d["name"]} (Secure Boot)" {{',
            f"    volume {volume_id}",
            f"    loader {kernel}",
            f"    initrd {initrd}",
            f'    options "{options}"',
            "}",
            "",
        ])

    (refind_dir / "refind.conf").write_text("\n".join(stanzas))

    if Path("/sys/firmware/efi/efivars").is_dir():
        print("\n--- Queuing Local MOK for UEFI MokManager Enrollment on First Reboot ---")
        pw = user_cfg["password"]
        run(["mokutil", "--import", str(mok["der"])], input_text=f"{pw}\n{pw}\n", check=False)
        run([
            "efibootmgr", "--create",
            "--disk", disk,
            "--part", "1",
            "--label", f"rEFInd Secure Boot ({arch['family']})",
            "--loader", f"\\EFI\\refind\\{shim_name}",
        ], check=False)

    if os.path.ismount(boot_mnt):
        run(["umount", str(boot_mnt)])
    run(["umount", str(efi_mnt)])


def main():
    if os.geteuid() != 0:
        sys.exit("Run this installer as root.")
    prepare_installer_cache()
    preflight_check()
    disk = input("\nEnter target block device (e.g., /dev/nvme0n1 or /dev/sda): ").strip()
    check_and_wipe_disk(disk)
    selected_distros = select_distros(single_mode=False)
    user_cfg = prompt_user_config()
    mok = generate_local_mok()
    p_efi, p_boot, p_btrfs = create_partitions(disk)
    create_btrfs_subvolumes(p_btrfs, selected_distros)
    boot_entries = []

    for distro in selected_distros:
        entry = bootstrap_distro(p_efi, p_boot, p_btrfs, distro, user_cfg, mok)
        boot_entries.append(entry)

    setup_refind_and_shim(disk, p_efi, p_boot, p_btrfs, boot_entries, mok, user_cfg)
    print("\n=== Installation Complete ===")
    print("Btrfs hierarchy ready: shared/@projects, shared/@containers, shared/@libvirt (nodatacow), shared/@swap")


if __name__ == "__main__":
    main()
