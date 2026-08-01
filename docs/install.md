# Installing playstick

Every command, in order, from a bare ASUS VivoStick to a projector that shows a clock,
accepts AirPlay, and plays films off the NAS.

Commands marked **control** run on your machine; **device** runs over SSH on the stick.
Everything under `./actl` is a control command that happens to execute inside the
container — the repo is mounted at `/work`, so paths are repo-relative either way.

- [0. What you need](#0-what-you-need)
- [1. The device, before Ansible](#1-the-device-before-ansible)
- [2. The control machine](#2-the-control-machine)
- [3. Point the inventory at the stick](#3-point-the-inventory-at-the-stick)
- [4. Secrets: the vault](#4-secrets-the-vault)
- [5. Choose the configuration](#5-choose-the-configuration)
- [6. Test clips](#6-test-clips-optional)
- [7. Provision](#7-provision)
- [8. Reboot, once, and it matters](#8-reboot-once-and-it-matters)
- [9. Verify](#9-verify)
- [10. Measure](#10-measure)
- [11. Day two](#11-day-two)
- [12. When it goes wrong](#12-when-it-goes-wrong)

---

## 0. What you need

| | |
|---|---|
| Device | ASUS VivoStick TS10 (Atom x5-Z8350, 2 GB, 32 GB eMMC) |
| On it | **Ubuntu Server** 26.04, no desktop. 2 GB of RAM does not have a GUI in it |
| Network | USB Ethernet strongly preferred over the SDIO Wi-Fi; same L2 segment as the phones, because AirPlay discovery is mDNS |
| Display | An HDMI display. This repo is tuned for a Panasonic PT-AE4000 projector |
| Control machine | Anything with **Docker** and an SSH client. Nothing else is installed on it |
| Optional | A NAS with an SMB/CIFS share for the movie library |

The control machine needs Docker and nothing else — no Ansible, no ffmpeg, no Python.
The image carries all of it.

---

## 1. The device, before Ansible

Install Ubuntu Server, then, **device**:

```bash
sudo apt update && sudo apt install -y openssh-server
ip -brief addr                 # note the address; it goes in inventory.yml
id                             # the login user must be able to sudo
```

Ansible needs nothing else on the device: `python3` is in the base install, and every
package the roles want is installed by the roles.

Two things to know before you start, both of which bite later if you do not:

**Ubuntu 26.04's `sudo` is sudo-rs**, which ignores the prompt Ansible passes with `-p`,
so `become` dies after 60 s with `Timeout (62s) waiting for privilege escalation prompt`.
`group_vars/all.yml` already points Ansible at classic sudo (`ansible_become_exe:
/usr/bin/sudo.ws`). Nothing on the device changes. If `/usr/bin/sudo.ws` does not exist
on your install, remove that line and expect the timeout.

**The stick's display is the projector.** From the moment `claim_tty1` masks `getty@tty1`
there is no local console on tty1; tty2 remains as a rescue login.

---

## 2. The control machine

**control**:

```bash
git clone <this repo> playstick && cd playstick
./actl 'ansible --version'       # builds the image on first run, ~2 min
```

Then set up SSH **from the host, not the container** — this is the one step that cannot
happen inside it:

```bash
ssh-copy-id simukka@10.0.1.228   # or your user@address
ssh simukka@10.0.1.228 true      # records the host key in ~/.ssh/known_hosts
```

Both matter. `~/.ssh` is mounted **read-only** so the container can never rewrite your
keys or `known_hosts`, and `ansible.cfg` keeps `host_key_checking = True` — so an
unknown host key is a hard failure with no way to accept it from inside. SSH once from
the host and the problem is gone.

If you would rather not use keys, `sshpass` is in the image:

```bash
./actl 'ansible-playbook site.yml -K'
```

### What `./actl` does

It wraps `docker compose run`, builds the image on first use, forwards `$SSH_AUTH_SOCK`
so a passphrase never enters the container, and picks the container user that maps to
*you* so files written into the mounted repo come back yours. Which user that is depends
on your Docker:

- **rootful Docker** — container UID *n* is host UID *n*, so the image builds a user
  matching yours.
- **rootless Docker** — the namespace maps container UID 0 to your host UID, and every
  other UID to a subuid with no claim on your files. There, container **root** is the
  one that maps back to you; `actl` detects this (`docker info` reports
  `name=rootless`) and adds `--user root`.

Get this wrong and the symptom is `Permission denied` or `Destination not writable` on
exactly the operations that write into the repo: `ansible-vault create`,
`make-testclip.sh`, and `fetch-results.yml` pulling into `results/`. `actl` handles it;
raw `docker compose run` does not.

---

## 3. Point the inventory at the stick

**control** — edit `inventory.yml`:

```yaml
all:
  hosts:
    vivostick:
      ansible_host: 10.0.1.228        # your device
      ansible_user: simukka           # login user with sudo
      ansible_python_interpreter: /usr/bin/python3
```

```bash
./actl 'ansible -m ping vivostick'
```

Expect `"ping": "pong"`. If this fails, fix it before going further — nothing later
diagnoses connectivity better than this does.

**`vivostick` here is a host, not a group.** Remember it; [section 4](#4-secrets-the-vault)
turns on it.

---

## 4. Secrets: the vault

Needed only for the NAS movie library — a share with authentication needs
`nas_username` / `nas_password`, and those must not go in `group_vars/all.yml`, which is
in git. Skip this section entirely if you have no NAS or the share is guest-readable.

### Where the file goes, and why it is not obvious

```
host_vars/vivostick/vault.yml         <- correct
group_vars/vivostick/vault.yml        <- NEVER LOADED, silently
group_vars/all/vault.yml              <- loads, and SHADOWS group_vars/all.yml
```

`vivostick` is a host. `group_vars/` is keyed by *group* name, so a directory named after
a host is simply never read — your credentials are ignored, the mount goes up as guest,
and nothing says a word. And `group_vars/all/` is worse: a **directory** named `all` takes
precedence over the **file** `all.yml`, so creating it silently removes every tunable in
`group_vars/all.yml` from the play — display mode, decoder, output path, the lot.

Both were verified by putting a marker variable in each location and asking Ansible which
survived. Use `host_vars/vivostick/`, and rename the directory too if you rename the host.

### Create it

**control**:

```bash
mkdir -p host_vars/vivostick
./actl 'ansible-vault create host_vars/vivostick/vault.yml'
```

That opens an editor inside the container (`nano`; override with
`docker compose run -e EDITOR=vim ansible ...`) and encrypts the result on save. Put in
it:

```yaml
---
nas_username: "kyle"
nas_password: "the share password"
# nas_domain: "WORKGROUP"     # only if your NAS wants one
```

`ansible-vault create` and `edit` need a terminal — do not pipe them into anything.

### Or write it in your own editor and encrypt afterwards

Sometimes easier, and it keeps your editor on the host:

```bash
$EDITOR host_vars/vivostick/vault.yml                              # plaintext for now
./actl 'ansible-vault encrypt host_vars/vivostick/vault.yml'       # now it isn't
```

### The rest of the vault commands

```bash
./actl 'ansible-vault view   host_vars/vivostick/vault.yml'   # read without decrypting on disk
./actl 'ansible-vault edit   host_vars/vivostick/vault.yml'   # decrypt, edit, re-encrypt
./actl 'ansible-vault rekey  host_vars/vivostick/vault.yml'   # change the password
./actl 'ansible-vault decrypt host_vars/vivostick/vault.yml'  # back to plaintext
```

A single value rather than a whole file — useful for putting one secret inside an
otherwise readable file:

```bash
./actl 'ansible-vault encrypt_string --stdin-name nas_password'
# type the secret, then ctrl-D; paste the !vault block into any vars file
```

### Using it

Once *any* vault file exists, every Ansible command needs the password, including
`--syntax-check`. Without one you get
`ERROR: Attempting to decrypt but no vault secrets found.`

```bash
./actl 'ansible-playbook site.yml --ask-vault-pass -K'
```

To stop typing it on every run, put the password in a file:

```bash
printf 'your-vault-password\n' > .vault-pass
chmod 600 .vault-pass                    # .gitignore already covers it
./actl 'ANSIBLE_VAULT_PASSWORD_FILE=.vault-pass ansible-playbook site.yml -K'
```

That is a plaintext password on your disk, protected by nothing but file permissions.
It is a reasonable trade on a personal control machine and a bad one on a shared box.
The encrypted `vault.yml` **is** meant to be committed; `.vault-pass` never is.

---

## 5. Choose the configuration

Everything lives in `group_vars/all.yml`, and it is heavily commented — read it once.
The decisions that actually matter on a first install:

```yaml
drm_force_mode: "1280x720@60"    # your display's mode. Measured, not preferred; see README
drm_force_connector: HDMI-A-1
uxplay_output_path: kms          # kms | cage | none -- which receiver runs
uxplay_advert_name: Projector    # what iOS shows in the AirPlay list
nas_server: "10.0.1.5"           # both empty skips the movie library entirely
nas_share: "movies"
player_audio: false              # films play SILENTLY until HDMI audio is probed
```

Three of those have consequences worth knowing before the run rather than after:

- **`drm_force_mode`** is set from a measured failure, not a preference. Leaving it empty
  uses the EDID-preferred mode, which on the PT-AE4000 is 1080**i** — that puts a
  deinterlacer in the path. `uxplay_request_size` derives from this value, so the two
  cannot drift apart.
- **`uxplay_output_path: kms`** is enabled *and started* by the play. `cage` is the
  compositor path and is incompatible with the idle clock — the `idle` role will stop
  the run and tell you to set `idle_enabled: false`. `none` installs both units and
  starts neither.
- **`nas_server`/`nas_share` empty** skips the NAS and player roles cleanly. You can turn
  the library on later with a re-run.

---

## 6. Test clips (optional)

Only needed for the measurement sweeps in [section 10](#10-measure). They are built on
the control machine because x264 on a 1.44 GHz Airmont would take longer than the
measurement they feed.

**control**:

```bash
./actl ./scripts/make-testclip.sh              # ~114 MB, 60 s clips
DURATION=30 ./actl ./scripts/make-testclip.sh  # half the size, faster sweeps
```

They land in `roles/probe/files/` (gitignored) and are shipped to the device by the
`probe` role. Without them the role warns and installs the scripts anyway.

---

## 7. Provision

**control** — dry run first. `-K` because sudo on the device wants a password:

```bash
./actl 'ansible-playbook site.yml --check --diff -K'
```

`--check` is honest here rather than decorative, but it is not free of noise: tasks that
read the device to decide something (the DRM node probe, `vainfo`, `systemd-escape`)
report differently when nothing has been installed yet. A clean first `--check` run on a
bare device is not expected.

Then the real thing:

```bash
./actl 'ansible-playbook site.yml -K'
```

Add `--ask-vault-pass` if you made a vault file. Expect 10–20 minutes on first run: it is
a 1.44 GHz Atom installing a few hundred packages and purging 109 more.

The play runs, in order: `base` → `trim` → `graphics` → `uxplay` → `idle` → `nas` →
`player` → `probe`. Two of those stop the run deliberately rather than shipping something
that limps:

- `graphics` asserts VA-API hardware H.264 decode exists (`VAProfileH264High :
  VAEntrypointVLD`). It is a health check on the driver stack — the shipped decoder is
  software, for reasons the README's Results section measures.
- `trim` asserts its purge list against `trim_protected_packages` before removing
  anything, because this role deletes packages from a device reachable only over SSH.

Narrow the run while iterating. There are no tags in this repo, so the levers are these:

```bash
./actl 'ansible-playbook site.yml -K --start-at-task="Install the player"'
./actl 'ansible-playbook site.yml -K --step'                    # confirm each task
./actl 'ansible-playbook site.yml -K -e trim_enabled=false'     # skip a whole role
```

`trim_enabled`, `idle_enabled`, `nas_enabled`, `player_enabled` and
`uxplay_output_path: none` each switch off one role's worth of work, which is usually a
faster way to isolate something than starting mid-play.

---

## 8. Reboot, once, and it matters

**device**:

```bash
sudo reboot
```

Not optional on a first provision. `drm_force_mode` is written to the kernel cmdline via
`/etc/default/grub`, and until the box reboots the display is still in its old mode —
every measurement you take before this is of a configuration you are not going to run.

Confirm it took, **device**:

```bash
cat /proc/cmdline | tr ' ' '\n' | grep video=      # video=HDMI-A-1:1280x720@60
```

---

## 9. Verify

**device** — the projector should already show the clock, and the stick should be in the
iOS AirPlay list as `Projector`.

```bash
systemctl status uxplay-kms.service uxplay-idle.service playstick-web.service
systemctl is-enabled uxplay-kms.service uxplay-cage.service   # enabled / disabled
journalctl -u uxplay-kms -b --no-pager | tail -30
```

```bash
# the display: connector, mode, and whether the FIFO is complaining
modetest -c 2>/dev/null | grep -A3 'HDMI-A-1'
dmesg | grep -i 'fifo underrun'                    # silence is the good answer
```

```bash
# the movie library, if you configured one
systemctl status srv-movies.automount
ls /srv/movies                                     # triggers the automount
curl -s localhost:8080/healthz                     # the web UI
```

From a phone on the same LAN: `http://vivostick.local:8080/` for the films, or the
AirPlay picker for mirroring.

**control** — one command for the whole picture, written to `results/`:

```bash
./actl 'ansible-playbook fetch-results.yml -K'
```

---

## 10. Measure

The facts probe is cheap and safe to run any time. The sweeps own the display for their
duration and stop the receiver to get it.

**control**:

```bash
# hardware, driver, elements, display, network. Add an iperf3 server for throughput:
docker compose up iperf                                    # in another terminal
./actl 'ansible-playbook fetch-results.yml -K -e probe_iperf_server=10.0.1.10'

# the decoder x sink x resolution matrix. ~20 minutes, owns the display.
./actl 'ansible-playbook fetch-results.yml -K -e run_matrix=true'

# narrow it
./actl 'ansible-playbook fetch-results.yml -K -e run_matrix=true \
        -e probe_decoders=avdec_h264 -e probe_sinks=fakesink,kms-default'

# the mpv sweep that settles player_vo / player_hwdec
./actl 'ansible-playbook fetch-results.yml -K -e run_player_probe=true'
```

Results land in `results/` on the control machine. The playbook stops the receiver and
the player before a sweep and starts them again afterwards, so the projector is not left
dark. What the columns mean, and which of them to distrust, is in the README under
[Reading the matrix](../README.md#reading-the-matrix).

---

## 11. Day two

**control**:

```bash
# change something in group_vars/all.yml, then re-run. Idempotent.
./actl 'ansible-playbook site.yml -K --diff'

# switch the output path for one run, to try it
./actl 'ansible-playbook site.yml -K -e uxplay_output_path=cage -e idle_enabled=false'

# the web UI on your laptop, no device involved
docker compose up gui                              # http://localhost:8080/
```

**device** — rollback, in decreasing order of severity:

```bash
systemctl disable --now uxplay-kms.service uxplay-cage.service   # stop receiving
systemctl disable --now uxplay-idle.service                      # stop the clock
systemctl disable --now playstick-web.service                    # stop the film player
systemctl disable --now srv-movies.automount                     # unmount the library
systemctl unmask --now getty@tty1.service                        # local console back
```

The next `site.yml` run undoes all of that. To make it stick, set `uxplay_output_path:
none`, `idle_enabled: false`, `player_enabled: false` or `nas_enabled: false` and re-run.

Patching is manual by design — `unattended-upgrades` is disabled because an apt run on
this CPU visibly disrupts a live mirroring session:

```bash
sudo apt update && sudo apt full-upgrade
```

---

## 12. When it goes wrong

| symptom | cause and fix |
|---|---|
| `Timeout (62s) waiting for privilege escalation prompt` | sudo-rs ignores Ansible's `-p` prompt. `ansible_become_exe: /usr/bin/sudo.ws` in `group_vars/all.yml` is the fix and is already set |
| `Permission denied (publickey,password)` | No key on the device. Fix on the **host**: `ssh-copy-id user@host`. The container's `~/.ssh` is read-only by design |
| `Host key verification failed` / `Are you sure you want to continue connecting` | SSH to the device once from the host so the key is recorded. The container cannot write `known_hosts`. If the host already knows the key, check the container agrees: `./actl 'ssh-keygen -F <ip>'`. OpenSSH resolves `~` from the passwd database and ignores `$HOME`, so `~/.ssh` is mounted at both `/home/ansible/.ssh` and `/root/.ssh` — whichever user `actl` picked has to find it |
| `Attempting to decrypt but no vault secrets found` | A vault file exists; add `--ask-vault-pass` or `ANSIBLE_VAULT_PASSWORD_FILE=.vault-pass` |
| Vault credentials appear to be ignored, share mounts as guest | The file is in `group_vars/vivostick/`, which is never read. Move it to `host_vars/vivostick/` |
| Every tunable seems to have reverted to its role default | There is a `group_vars/all/` **directory** shadowing `group_vars/all.yml`. Delete the directory |
| `Destination '/work/...' not writable`, `Permission denied` writing into the repo | Rootless Docker with the wrong container user. Use `./actl`, which detects it — not a raw `docker compose run` |
| `ansible-vault create` → `not a tty, editor cannot be opened` | Something is piping or redirecting the command. Run it plainly |
| `No DRM node bound to i915 was found` | Check `dmesg \| grep -i i915` on the device. `card0` is often simpledrm rather than the GPU; the role prefers the PCI by-path link |
| `No VA-API H.264 decode entrypoint found` | The `graphics` gate. Run `vainfo --display drm --device /dev/dri/renderD128` on the device as root — outside the `render` group it reports nothing at all, which looks identical to a broken install |
| Projector shows the boot log and a cursor | The idle clock is not running: `systemctl status uxplay-idle`. If `uxplay_output_path` is `cage`, it never can — that is enforced, not a bug |
| Nothing in the iOS AirPlay list | A film is playing (UxPlay is stopped for its duration, by design), or the phone is on a different L2 segment — discovery is mDNS and does not route |
| Films play but silently | `player_audio: false` is the shipped default. See the README on `hdmi-lpe-audio` before changing it |
| `No test clips found in roles/probe/files/` | Run `./actl ./scripts/make-testclip.sh`, then re-run the play |

More detail on any of the measured decisions is in the [README](../README.md), and the
first-person account of how the matrix was built — including several ways it fooled its
author — is in [matrix-narrative.md](matrix-narrative.md).
