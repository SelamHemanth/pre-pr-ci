# <img src="web/static/logo.svg" alt="Pre-PR CI Logo" height="40" /> Pre-PR CI

This tool automates distribution detection, configuration, patch application, and kernel build/test workflows across supported Linux distributions.

---

## ✨ Features
- Automatic detection of target distribution
- Distro-specific build scripts
- Patch management and Pre-PR CI integration
- Automated kernel boot testing on remote VMs
- Unified interface via `make` targets
- Clean separation of logs, outputs, and patches
- Web dashboard with live logs, job history and an embedded terminal

---

## 📦 Supported Distributions
- **OpenAnolis**
- **OpenEuler**
- **OpenCloud** (`🚧 Implementing...`)

---

## 🌐 Web Interface

```bash
pip3 install --user -r web/requirements.txt
python3 web/server.py            # then open http://server-ip:5000
```

* Configure, build and test from the browser
* Live log streaming, job history that survives a restart, light/dark themes
* Everything is served locally, so it also works with no internet access

> **The server has no authentication and listens on every interface, and its
> terminal tab is a shell on the build machine.** Run it only on a network you
> trust, or bind it to localhost and use an SSH tunnel:
> `python3 web/server.py --host 127.0.0.1` then
> `ssh -L 5000:127.0.0.1:5000 you@buildhost`.

---

## ⚙️ Usage

- Install Prerequisite packages (Check in [DOCUMENT.md](https://github.com/SelamHemanth/pre-pr-ci/blob/master/DOCUMENT.md))

```bash
# The KABI tooling and the openEuler spec files are submodules, so clone
# with --recurse-submodules; check_kapi, check_kabi and rpm_build all fail
# with confusing errors if these directories are empty.
git clone --recurse-submodules https://github.com/SelamHemanth/pre-pr-ci.git
cd pre-pr-ci

# Already cloned without them?
git submodule update --init --recursive
```

### Command Line Interface

* `make config`             - Configure target distribution
* `make build`              - Build kernel
* `make test`               - Run distro-specific tests
* `make list-tests`         - List available tests for configured distro
* `make anolis-test=<name>` - Run specific OpenAnolis test
* `make euler-test=<name>`  - Run specific openEuler test
* `make clean`              - Remove logs/ and outputs/
* `make reset`              - Reset git repo to saved HEAD
* `make distclean`          - Remove all artifacts and configs
* `make update-tests`       - Change which tests are enabled
* `make update`             - Pull a newer version of this tool

### Service Management

Run the web interface as a system service:

* `sudo ./service.sh install`   - Install as systemd service
* `sudo ./service.sh start`     - Start service
* `sudo ./service.sh status`    - Check status
* `sudo ./service.sh logs`      - View logs
* `sudo ./service.sh stop`      - Stop service
---

## 📖 Documentation

For detailed documentation, please refer to: [DOCUMENT.md](https://github.com/SelamHemanth/pre-pr-ci/blob/master/DOCUMENT.md)

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

This project is licensed under the GPL-3.0 License - see the [LICENSE](https://github.com/SelamHemanth/pre-pr-ci/blob/master/LICENSE) file for details.

---

## 👤 Author

**Hemanth Selam**
- GitHub: [@SelamHemanth](https://github.com/SelamHemanth)
- Email: Hemanth.Selam@amd.com
