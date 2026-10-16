![playstick](logo.svg)

**Old tech meets modern software.**

Playstick turns an **ASUS VivoStick TS10** into a compact, dedicated media player 
that plugs directly into a projector's HDMI port.

It provisions and measures a minimal Ubuntu Server installation with video rendered 
directly through the Intel integrated graphics stack **without an X server or desktop environment**.

## What makes it different

* **No desktop, no X server.** Video goes straight to the Intel GPU over KMS/DRM via `cage`/`seatd` on `tty1`.
* **Fully provisioned by Ansible over SSH.** The device holds no state you can't rebuild; every tunable lives in `group_vars`, and the control node ships as a Docker image.
* **AirPlay mirroring receiver** built on [`UxPlay`](https://github.com/simukka/UxPlay) mirror any Apple device straight to the projector.
* **Child-friendly web movie library.** Plays films off a read-only CIFS NAS automount from a browser UI simple enough for a kid.
* **It controls the projector itself.** RS-232 switches the projector on and off, and an idle framebuffer clock shows when nothing is playing.

## Everyone can hear their own language

Every viewer opens the library on **their own device**, picks a language, and listens on their own headphones.
Each device pins its audio to the film's master clock, so the whole room stays in sync (a skip re-syncs everyone automatically). 
Two people watching the same film can hear it in different languages at once.

Great for multi-language households, friends, and parties.
