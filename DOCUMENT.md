# Patch Pre-Check CI Tool Documentation

## Overview

The Patch Pre-Check CI Tool is an automated testing framework designed to validate Linux kernel patches across different distributions before submission. It streamlines patch validation by automating patch application, kernel builds, distribution-specific testing.

## Features

- **Automated Patch Processing:** Generates patches from git commits and applies them sequentially
- **Incremental Build Testing:** Tests each patch individually to identify build-breaking changes early
- **Comprehensive Test Suite:** Configuration validation, multiple build configurations,distribution-specific testing
- **Smart Configuration:** Interactive wizard with sensible defaults
- **Password Management:** Secure storage of host and VM credentials for unattended testing
- **Clean Workflow:** Automatic git state management with rollback
- **VM Boot Verification:** Automated kernel installation, boot, and version verification on remote VMs

## Supported Distributions

| Distribution | Target Kernel         | Kernel Versions     | Architectures  | Boot Testing    |
|--------------|-----------------------|---------------------|----------------|-----------------|
| OpenAnolis   | ANCK (Cloud Kernel)   | Multiple LTS        | x86_64, aarch64| ✅ Full         |
| openEuler    | openEuler Kernel      | Multiple LTS        | x86_64, aarch64| 🚧 Implementing |

## Installation & Setup

### Prerequisites

```bash
sudo yum install -y git make gcc flex bison elfutils-libelf-devel openssl-devel ncurses-devel bc rpm-build
```
OpenAnolis extra requirements
```bash
sudo yum install -y audit-libs-devel binutils-devel libbpf-devel libcap-ng-devel libnl3-devel newt-devel pciutils-devel xmlto yum-utils
```

For VM boot testing:
```bash
sudo yum install -y sshpass
```

### Getting Started

```bash
git clone https://github.com/SelamHemanth/patch-precheck-ci.git
cd patch-precheck-ci

make config # Run config wizard
make build # Build/test patches
make test # Execute all tests
```

### Configuration Steps

1. **Distribution Selection**

    ```
    ╔════════════════════════╗
    ║ Distribution Selection ║
    ╚════════════════════════╝
    Detected Distribution: anolis
    Available distributions:
      1) OpenAnolis
      2) openEuler
    Enter choice [1-2]:
    ```

2. **Distribution-Specific Configuration**

   **OpenAnolis:**
     - Linux source code path
     - Signed-off-by name/email
     - Anolis Bugzilla ID (ANBZ)
     - Number of patches to apply (from HEAD)
     - Build threads (default: CPU cores)

     **Test Options:**

     | Test                        | Description                  | Purpose                                  |
     |-----------------------------|------------------------------|------------------------------------------|
     | check_Kconfig               | Validate Kconfig settings    | Ensures config validity                  |
     | build_allyes_config         | Build with allyesconfig      | Compile w/ all enabled options           |
     | build_allno_config          | Build with allnoconfig       | Minimal kernel build                     |
     | build_anolis_defconfig      | Build with anolis_defconfig  | Production default config                |
     | build_anolis_debug_defconfig| Build with debug config      | Enable debugging features                |
     | anck_rpm_build              | Build ANCK RPM packages      | RPMs for installation                    |
     | check_kapi                  | Check KAPI compatibility     | ABI compatibility checks                 |
     | boot_kernel_rpm             | Automated VM boot test       | Install, boot, and verify kernel on VM   |

     Enable: individual (e.g. 1,3,5), all, or none.

   **Password Configuration** (when RPM build or boot test enabled)

   **Host Configuration:**
   - Host sudo password (for installing build dependencies)
   - Stored securely for unattended testing
   - Used for: package installation, yum-builddep

   **VM Configuration** (when boot test enabled):
   - VM IP address (bridge or local network)
   - VM root password (for SSH/SCP access)
   - Used for: RPM transfer, installation, reboot, verification

   **openEuler:**
     - Linux source code path
     - Signed-off-by name/email
     - Euler Bugzilla ID
     - Patch category (deafult: feature)
     - Number of patches to apply (from HEAD)
     - Build threads (default: CPU cores)

     **Test Options:**

     | Test              | Description                       | Purpose                                |
     |-------------------|-----------------------------------|----------------------------------------|
     | check_licence     | Validate license headers          | Ensures proper licensing compliance    |
     | build_oedefconfig | Build with openeuler_defconfig    | Production default config              |
     | build_allmod      | Build with allmodconfig           | Compile with all modules enabled       |
     | check_patch       | Run checkpatch.pl validation      | Verify coding style and patch format   |
     | check_format      | Check code formatting             | Ensures code style consistency         |
     | check_kabi        | Check KABI compatibility          | ABI compatibility checks               |
     | rpm_build         | Build openEuler RPM packages      | RPMs for installation                  |
     | boot_kernel       | Boot test (requires remote setup) | Install, boot, and verify kernel on VM |

     Enable: individual (e.g. 1,3,5), all, or none.

   **Password Configuration** (when RPM build or boot test enabled)

   **Host Configuration:**
   - Host sudo password (for installing build dependencies)
   - Stored securely for unattended testing
   - Used for: package installation, yum-builddep

   **VM Configuration** (when boot test enabled):
   - VM IP address (bridge or local network)
   - VM root password (for SSH/SCP access)
   - Used for: RPM transfer, installation, reboot, verification

## Make Targets

| Target        | Description                            |
|---------------|----------------------------------------|
| config        | Interactive configuration wizard       |
| build         | Generate/apply patches & build         |
| test          | Run distribution-specific test suite   |
| clean         | Remove logs/build outputs              |
| reset         | Reset git to saved HEAD                |
| distclean     | Remove all artifacts/config            |
| mrproper      | Complete cleanup                       |
| help          | Display usage info                     |

## Security Considerations

### Password Storage
- Passwords are stored in plaintext in `.configure` file
- File permissions should be restricted: `chmod 600 anolis/.configure`
- **Do not commit `.configure` to version control**
- Consider using SSH keys for production environments

### SSH Configuration
- Currently uses `StrictHostKeyChecking=no` for automation
- For production, configure proper SSH key authentication
- Use known_hosts verification in secure environments

## Contributing

To add a new distribution:

- Create: `newdistro/` directory
- Add: `config.sh`, `build.sh`, `test.sh`, `Makefile`
- Update: main `Makefile` with new targets
- Implement boot testing if supported
- Thoroughly test all features
- Submit pull request

## License

GPL-3.0 licence

This tool is provided as-is for kernel development and testing purposes.

## Support

- [Repository](https://github.com/SelamHemanth/patch-precheck-ci)
- Issues via GitHub

---
