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

![The phone UI: the library, the remote, and choosing a language](screenshots/web-0-hero.png)

## The phone is the remote

Open the stick's address on any phone or tablet on the home network. There is nothing to install.

* **Tap a poster to play it.** The projector switches itself on, and the page says so while the lamp warms up.
* **A series is one tile.** Tapping it opens the seasons and their episodes.
* **Sort and filter** by title, year, or genre.
* **Big, simple controls** while a film plays: skip ten seconds, pause, stop, and volume.

<details>
<summary>More screenshots</summary>

![The library grid](screenshots/web-1-grid.png)
![A series and its episodes](screenshots/web-2-series.png)
![Sorting and filtering](screenshots/web-3-filter.png)
![Waiting for the projector to warm up](screenshots/web-4-preparing.png)
![The remote while a film plays](screenshots/web-5-playing.png)
![Choosing sound and language](screenshots/web-6-sound.png)
![The library on a tablet](screenshots/web-7-tablet-grid.png)

</details>

## A desktop app to run the library

The heavy lifting happens on your own computer, not the stick. The **Playstick Admin** app (PySide6, `make admin`) 
scans your folders of films, lets you choose what goes on the stick, and prepares each film in the background: 
probing, re-encoding where the stick needs it, extracting per-language audio, and matching it on TMDb.

![Prepare tab: choose films from your sources and watch the queue](screenshots/1-prepare.png)

* **Prepare.** Every film on every source, in one sortable table. Select what you want and add it to the library; 
  the queue works through them one at a time and survives the app being closed.
* **Library.** What the stick is actually holding, with posters. Fix a title, genre or rating, or correct a bad TMDb match, 
  and the change goes straight to the stick.
* **Device.** Whether the stick is reachable, playback and volume controls, and recovery for films that didn't get through.
* **Config.** Edit the Ansible variables in `group_vars` with a form, and comments in the YAML are preserved.

![Library tab: correct a film's metadata or TMDb match](screenshots/3-library-film.png)

<details>
<summary>More screenshots</summary>

![Library tab](screenshots/2-library.png)
![Device tab](screenshots/4-device.png)
![Config tab](screenshots/5-config.png)

</details>

*All screenshots use a demo library of public-domain films.*
