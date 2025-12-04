# Patch Pre-Check CI Tool

This tool automates distribution detection, configuration, patch application, and kernel build/test workflows across supported Linux distributions.

---

## ✨ Features
- Automatic detection of target distribution
- Distro-specific build scripts
- Patch management and pre-check CI integration
- Automated kernel boot testing on remote VMs
- Password-based authentication for unattended testing
- Unified interface via `make` targets
- Clean separation of logs, outputs, and patches

---

## 📦 Supported Distributions
- **OpenAnolis**
- **OpenEuler** (`Implementing...`)

---

## ⚙️ Usage

- Install Prerequisite packages (Check in [DOCUMENT.md](https://github.com/SelamHemanth/patch-precheck-ci/blob/main/DOCUMENT.md))

```bash
# Clone repository
git clone https://github.com/SelamHemanth/patch-precheck-ci.git

# Step into investigation
cd patch-precheck-ci
```

* `make config`     - Configure target distribution
* `make build`      - Build kernel
* `make test`       - Run distro-specific tests
* `make clean`      - Remove logs/ and outputs/
* `make reset`      - Reset git repo to saved HEAD
* `make distclean`  - Remove all artifacts and configs
* `make mrproper`   - Same as distclean

---

## 📖 Documentation

For detailed documentation, please refer to: [DOCUMENT.md](https://github.com/SelamHemanth/patch-precheck-ci/blob/main/DOCUMENT.md)

---

## 🤝 Contributing

Contributions are welcome! To contribute:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Commit your changes (`git commit -am 'Add new feature'`)
4. Push to the branch (`git push origin feature/your-feature`)
5. Open a Pull Request

---

## 📄 License

This project is licensed under the GPL-3.0 License - see the [LICENSE](https://github.com/SelamHemanth/patch-precheck-ci/blob/main/LICENSE) file for details.

---

## 👤 Author

**Hemanth Selam**
- GitHub: [@SelamHemanth](https://github.com/SelamHemanth)
- Email: Hemanth.Selam@amd.com

---

## 📝 Note

For **OpenEuler**, support is currently being implemented. Now it supports `make config`,`make build`&`make clean`. `make test` is being implemented now.
