
Multi-Distro Btrfs Developer Workstation & KVM Bootstrapper (arm64 & amd64)

An automated Python installation, maintenance, and virtualization toolkit for building a dual-architecture (arm64/aarch64 and amd64/x86_64) multi-distro developer workstation on a single Btrfs storage pool.
Designed for developers who frequently hop across Linux distributions (Fedora, Debian, Alpine) and standard libraries (glibc ↔ musl) while keeping source code, OCI containers, and Windows 11 KVM virtual machines completely isolated from OS rollbacks and reinstalls.

## Key Features

- Dual-Architecture Support (arm64 & amd64): Automatically detects host CPU architecture (aarch64/arm64 or x86_64/amd64) and configures matching UEFI shims (`shimaa64.efi` / `shimx64.efi`), rEFInd binaries, package architectures, QEMU machine types (`virt` / `q35`), and UEFI VM firmware (`AAVMF` / `OVMF`).
- Automated Preflight & Self-Bootstrapping (`preflight_check`): Validates UEFI boot mode, verifies `btrfs-progs >= 6.1`, auto-installs missing host CLI tools (`debootstrap`, `sbsigntools`, `mokutil`, `efibootmgr`) via `dnf`/`apt-get`, and stages upstream rEFInd (0.14.2) binaries and Btrfs EFI drivers.
- Folder-Isolated Btrfs Subvolume Hierarchy: Every OS lives inside its own top-level directory (`<os_id>/@*`) in `subvolid=5`, eliminating cross-distro file collisions while sharing `shared/@projects`, `shared/@containers`, `shared/@libvirt`, and `shared/@swap`.
- End-to-End UEFI Secure Boot: Chains Microsoft-CA-signed shim → locally MOK-signed rEFInd (`grub<arch>.efi`) + MOK-signed `btrfs_<arch>.efi` → signed kernels. Fedora and Debian support automatic module signing with akmods/DKMS; Alpine re-signs LTS kernel updates through `kernel-hooks`.
- Fedora RPM Fusion: Enables the official RPM Fusion free/nonfree repositories and installs their AppStream metadata so GNOME Software and Discover can display repository apps.
- Automated Snapper Baseline & Rollbacks: Fedora and Debian initialize Snapper (`root` config) mapped to `<os_id>/@snapshots` and capture an immediate clean baseline; Fedora also adds DNF5 transaction hooks. Alpine 3.24 does not package Snapper, so its `@snapshots` subvolume remains empty.
- Debian APT Sources: Enables `main`, `contrib`, `non-free`, and `non-free-firmware` for Debian package and security repositories in every install.
- Flatpak & Flathub: Installs Flatpak on Fedora, Debian, and Alpine, and enables the system-wide Flathub remote for each installation.
- Snap: Installs `snapd`, enables `snapd.socket`, and configures the `/snap` classic path on Fedora and Debian; maintenance can apply this to existing systems.
- Desktop Software Stores: Selects GNOME Software or Plasma Discover per desktop, with Flatpak/Snap backends where packaged. Fedora's GNOME Software build includes Flatpak support but does not package its Snap plugin.
- Debian ExtRepo: Installs Debian's curated external repository manager on both Debian desktop variants; existing Debian systems can install it from maintenance.
- Alpine Network: Configures `eth0` for DHCP and enables OpenRC networking at boot so installed Alpine systems have network connectivity.
- Alpine Community Repository: Enables the Community repository before package installation for both Alpine variants.
- Alpine XFCE: Installs X.Org, LightDM's OpenRC service, `elogind`, and D-Bus; enables the services LightDM needs to start a graphical session.
- Alpine Kernel Updates: Installs `kernel-hooks` and signs updated LTS kernels with the enrolled MOK; the maintenance helper can repair an unsigned kernel after `setup-alpine`.
- Cross-Distro Windows 11 KVM Provisioning (`win11_vm.py`): Provisions NVMe-tuned qcow2 disks (`nodatacow`, `discard='unmap'`, `io='native'`) with UEFI Secure Boot (`AAVMF` / `OVMF`), virtual TPM 2.0 (`swtpm`), `virtio-win.iso`, and unattended `autounattend.xml` driver injection inside `shared/@libvirt`—re-attachable across any booted distro in one command.

## Supported Target Operating Systems (arm64 & amd64)

| ID | Distribution | Desktop / Compositor | Init / libc | Display Manager Subvolume |
| :--- | :--- | :--- | :--- | :--- |
| `fedora_kde` | Fedora KDE | KDE Plasma | `systemd` / `glibc` | `fedora_kde/@sddm` |
| `fedora_gnome` | Fedora GNOME | GNOME Workstation | `systemd` / `glibc` | `fedora_gnome/@gdm` |
| `debian_gnome` | Debian GNOME | GNOME | `systemd` / `glibc` | `debian_gnome/@gdm3` |
| `debian_kde` | Debian KDE | KDE Plasma | `systemd` / `glibc` | `debian_kde/@sddm` |
| `alpine_xfce` | Alpine Linux | XFCE4 | `OpenRC` / `musl` | *(Root-contained)* |
| `alpine_sway` | Alpine Linux | Sway (Wayland + `seatd`) | `OpenRC` / `musl` | *(TTY / `seatd`)* |

## Disk Partition & Btrfs Subvolume Architecture

### GPT Partition Table

| Partition | Size | Filesystem | Label | Mount Point / Purpose |
| :--- | :--- | :--- | :--- | :--- |
| Part 1 | `1024 MiB` | `FAT32` (ESP) | `EFI` | `/boot/efi` — Shared UEFI System Partition (`shim`, `rEFInd`, `MOK.der`) |
| Part 2 | `4 GiB` | `ext4` | `FEDORA_BOOT` | `/boot` — Dedicated boot partition for Fedora |
| Part 3 | `Remainder` | `Btrfs` | `MULTIBOOT_POOL` | Main Btrfs pool (`subvolid=5`) hosting `shared/` and `<os_id>/` folders |

### Btrfs Pool Layout (`subvolid=5`)

```text
MULTIBOOT_POOL (subvolid=5)
├── shared/
│   ├── @projects              -> /srv/projects (symlinked to ~/Projects, UID/GID 1000)
│   ├── @containers            -> /var/lib/containers (OCI images shared across distros)
│   ├── @libvirt               -> /var/lib/libvirt/images (nodatacow, VM disks + virtio-win.iso)
│   └── @swap                  -> /swap (nodatacow, holds per-distro swapfile_<os_id>)
├── fedora_kde/
│   ├── @                      -> /
│   ├── @home                  -> /home (isolated glibc/desktop dotfiles)
│   ├── @snapshots             -> /.snapshots
│   ├── @var_log               -> /var/log
│   ├── @var_cache             -> /var/cache
│   ├── @var_tmp               -> /var/tmp
│   └── @sddm                  -> /var/lib/sddm
├── debian_gnome/
│   └── (@, @home, @snapshots, @var_log, @var_cache, @var_tmp, @gdm3)
└── alpine_sway/
    └── (@, @home, @snapshots, @var_log, @var_cache, @var_tmp)
```

> Why isolate `@home` per distro? Sharing `/home` between `glibc` distros (Fedora, Debian) and `musl` distros (Alpine) causes compiled language caches (`~/.cargo`, `~/.npm`, Python `venv`) to fail with binary incompatibility errors. Isolating `<os_id>/@home` while mounting `shared/@projects` inside each OS gives you clean builds everywhere.

## Prerequisites & Live Environment

- Recommended Live ISO: Boot from a Fedora Workstation or KDE Live ISO (aarch64 or x86_64) in UEFI mode. Fedora Live works with UEFI Secure Boot enabled out of the box and includes `dnf5` and `btrfs-progs >= 6.1`.
- Fedora bootstrap seeds root account files for RPM lookup, then uses the live host's DNF configuration and repositories so the installed Fedora release follows the live environment's configured release.
- The rEFInd archive and VirtIO driver ISO are cached under `/var/tmp/multiboot_installer_cache` so they remain available if the install fails or the Live ISO's RAM-backed `/tmp` is full. The VirtIO ISO is copied into `shared/@libvirt` without removing the cache copy. Alpine's minirootfs archive and all large Fedora, Debian, and Alpine package caches are kept on target storage in `shared/@libvirt/.multiboot_installer_cache`. A full-disk install wipes that target-side cache; single-OS maintenance reinstalls can reuse it.
- Automatic Dependency Resolution: `preflight_check()` automatically detects your architecture, checks UEFI mode, installs missing host CLI tools (`debootstrap`, `sbsigntools`, `mokutil`, `efibootmgr`, `gdisk`, `parted`, `dosfstools`, `e2fsprogs`), and stages upstream rEFInd (0.14.2) before touching any disk.
- Verified Upstream Sources: The installer downloads the official direct URLs for rEFInd 0.14.2 from SourceForge and the stable `virtio-win.iso` from Fedora People, so the bootstrapper and VM helper match the actual upstream artifacts they claim to fetch.

## Usage Guide

### 1. Fresh Multi-Distro Installation

```bash
chmod +x multi_distro_installer.py multiboot_maint.py win11_vm.py
sudo ./multi_distro_installer.py
```

1. Preflight Check: Validates arm64/amd64 UEFI mode and resolves missing dependencies.
2. Target Block Device: Enter your drive (e.g., `/dev/nvme0n1`) and type `WIPE <device>` to confirm. Stale mounts under `/mnt/multiboot_target` are unmounted first; partitions mounted elsewhere or in active swap cause the installer to stop safely.
3. Target OS Selection: Enter comma-separated numbers of the distros to deploy.
4. Downloads: Reuses cached files and fetches missing files automatically, with no source-selection prompt.
5. Unified Developer User: Enforces `UID 1000` / `GID 1000` across all distros and sets user, root, and MOK enrollment passwords.

### 2. First-Boot Secure Boot MOK Enrollment

1. Reboot into your target drive.
2. `shim<arch>.efi` will automatically launch MokManager (blue UEFI screen).
3. Select `Enroll MOK` → `Continue` → `Yes`, enter your password, and reboot.
4. rEFInd will boot under full UEFI Secure Boot with menu entries for all installed systems.

### 3. Maintenance, Snapper Rollbacks & Single-OS Management

Run `multiboot_maint.py` from any booted distro or from a Live USB:

```bash
sudo ./multiboot_maint.py
```

1. Inspect Pool & Snapshots: Lists `shared/` and `<os_id>/` subvolumes, marks the currently booted OS, and shows Snapper snapshot IDs when available (Fedora and Debian).
2. Atomic Snapper Rollback: Moves `<os_id>/@` aside to `@_pre_rollback_<timestamp>` and snapshots your chosen Fedora or Debian Snapper ID into `<os_id>/@`.
3. Wipe Single OS: Deletes all snapshots, subvolumes, swapfile, and rEFInd stanza for one OS while keeping `shared/` untouched.
4. Repair Alpine Secure Boot & Sync Kernels: Signs Alpine LTS kernels with their enrolled MOK, installs the kernel-update signing hook, and updates `refind.conf`.
5. Add / Reinstall Single OS: Deploys a new distro folder (or cleanly replaces an existing one) using your existing enrolled MOK keys. The maintenance helper follows the same single-distro selector contract as the installer, so it resolves the chosen distro as the first item from the valid selection list.
6. Enable Alpine Community Repositories: Updates the repository configuration and package indexes for every installed Alpine system.
7. Install / Enable Snap: Installs `snapd` and enables `snapd.socket` for all installed Fedora and Debian systems.
8. Install Desktop Store Integrations: Installs the selected GNOME Software or Plasma Discover package and available Snap/Flatpak backends; for Fedora, also installs RPM Fusion AppStream metadata.
9. Install ExtRepo: Installs Debian's curated external repository manager for every installed Debian system.

### 4. Windows 11 KVM Guest Provisioning (arm64 & amd64)

Create a Windows 11 VM stored inside `shared/@libvirt` (`/var/lib/libvirt/images`):

```bash
sudo ./win11_vm.py --iso /srv/projects/Win11_24H2_English.iso --disk-gb 100 --ram-mb 12288 --vcpus 6
```

When you hop to another host distro (for example, from Fedora KDE to Alpine Sway or Debian GNOME), register the existing VM into that host's `libvirtd` in one command:

```bash
sudo ./win11_vm.py --register-only
```

## CI Quality & Typecheck Matrix

GitHub Actions (`.github/workflows/ci.yml`) runs on every push and pull request to `main`/`master` across a 2×2 matrix (Python 3.11 & 3.12 × simulated `aarch64` & `x86_64`):

1. Ruff Linter & Formatter: Enforces clean style and catches unused imports or syntax issues across the shared helper and all three entry-point scripts.
2. Mypy Static Typecheck: Validates type annotations across all four Python modules.
3. Dual-Arch Contract Smoke Test: Verifies architecture resolution (`aa64` / `virt` / `tpm-tis-device` vs `x64` / `q35` / `tpm-crb`) without side effects.

## Repository Structure

```text
.
├── .github/workflows/ci.yml    # Dual-arch CI matrix (Ruff lint/format, Mypy typecheck, contract tests)
├── .gitignore                  # Protects MOK private keys, caches, ISOs, VM images, and bytecode
├── multiboot_common.py         # Shared architecture, command, and download configuration
├── multi_distro_installer.py   # Preflight, GPT/Btrfs setup, chroot bootstrap, Snapper & Secure Boot
├── multiboot_maint.py          # Interactive pool inspector, Snapper rollback, rEFInd sync & single-OS add
├── win11_vm.py                 # Dual-arch Windows 11 KVM guest provisioner & cross-distro VM registrar
└── README.md                   # Architecture, usage & CI documentation
```

## License

MIT License. Use at your own risk—always verify target block device paths before confirming disk wipe operations.