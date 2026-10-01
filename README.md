# CEC-Sonos Bridge

Control your Sonos speakers with your TV remote using a Raspberry Pi and HDMI-CEC.

![Version](https://img.shields.io/badge/version-1.2.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Pi](https://img.shields.io/badge/Raspberry%20Pi-Zero%202%20W-red)

---

## What It Does

Press the **volume buttons on your TV remote** → Your **Sonos speaker** volume changes.

No apps. No voice commands. Just your normal TV remote controlling your Sonos.

### How It Works

```
TV Remote → HDMI-CEC → Raspberry Pi → WiFi → Sonos Speaker
  Vol+                      ↓                      ↓
  Vol-                   Python              Volume changes!
  Mute                   script
```

The Pi plugs into an HDMI port on your TV and pretends to be an "audio system." When you press volume on your TV remote, the Pi intercepts those commands via HDMI-CEC and sends them to your Sonos over WiFi.

---

## Features

- **Phone-friendly setup wizard** — No coding required
- **Auto hotspot mode** — Creates WiFi network if it can't connect
- **Admin panel** — Access at `sonosbridge.local`
- **TV splash screen** — Switch the TV to the bridge's input to see the admin panel's address and QR code
- **Auto-recovery** — Falls back to setup mode if WiFi is lost
- **OTA updates** — Update from the admin panel

---

## Hardware Required

| Item | Notes |
|------|-------|
| [Raspberry Pi Zero 2 W](https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/) | Must be the "2 W" version for WiFi |
| Micro SD Card (8GB+) | For the operating system |
| Micro HDMI to HDMI cable/adapter | Connects Pi to TV |
| USB-C power supply | 5V 2.5A recommended |
| Sonos speaker | Any Sonos speaker on your WiFi network |

**Total cost:** ~$25-40 (if you already have a power supply)

---

## Installation

### Step 1: Flash the SD Card

1. Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
2. Select **Raspberry Pi OS Lite (64-bit)**
3. Click the ⚙️ gear icon and configure:
   - Hostname: `sonosbridge`
   - Enable SSH: **Yes**
   - Username: `pi`
   - Password: *(your choice)*
   - WiFi: **Enter your home WiFi credentials**
4. Flash the SD card

### Step 2: Install the Software

1. Insert SD card into Pi
2. Connect Pi to your TV via HDMI
3. Power on and wait 2-3 minutes
4. SSH into the Pi:
   ```bash
   ssh pi@sonosbridge.local
   ```
5. Download and run the installer:
   ```bash
   cd ~
   git clone https://github.com/rhetoricdesigns/cec-sonos-bridge.git
   cd cec-sonos-bridge
   sudo bash install.sh
   ```
6. Reboot when prompted

### Step 3: Configure

1. Connect your phone to WiFi: **SonosBridge-Setup** (password: `sonosbridge`)
2. Open your browser to `http://192.168.4.1`
3. Select your home WiFi and enter the password
4. Click "Scan for Sonos Speakers" 
5. Select your Sonos speaker
6. Choose your HDMI port (avoid the ARC port)
7. Click "Complete Setup"

**Done!** Your TV remote now controls your Sonos volume.

---

## Admin Panel

After setup, access the admin panel at: **http://sonosbridge.local**

Features:
- Test volume control
- View status and logs
- Check for updates
- Rollback to previous versions
- Factory reset
- LG TV tab: LG mode, for LG TVs (see [LG TVs](#lg-tvs) below)

---

## Troubleshooting

### Can't find the Pi on my network
```bash
# Try finding it by IP
ping sonosbridge.local

# Or scan your network
arp -a | grep raspberry
```

### Volume buttons don't work
1. Make sure CEC is enabled on your TV (check TV settings)
2. Try a different HDMI port (avoid ARC/eARC ports)
3. Check the logs:
   ```bash
   ssh pi@sonosbridge.local
   sudo tail -f /var/log/cec-sonos-bridge.log
   ```

### TV switches to the Sonos Bridge screen
If it happens when you press Home or an app button on a Fire TV remote, check which HDMI input the Fire TV has saved: on the Fire TV, go to **Settings → Equipment Control → Manage Equipment**. It must match the port the Fire TV is plugged into. The remote uses it to switch the TV's input by infrared, so if it names the bridge's port, it sends the TV to the bridge.

The bridge itself never asks the TV to show it. It only sends a picture while you've chosen its input, and if the TV is sent there shortly after another device takes the screen, the bridge hands the screen straight back to that device (you may see a blank screen for a second). To look at the bridge's screen on purpose, pick its input more than a minute after using another device.

If it still happens, here is how to see why:
1. Make it happen (e.g. press Home on the Fire TV remote)
2. Open **http://sonosbridge.local/cec** (or Settings → CEC Activity in the admin panel)
3. Tap **Copy** and send the text with your bug report

It lists every message between the TV, your other HDMI devices and the bridge, newest first, and highlights any that move the TV to the bridge's input.

### TV says an audio device is on a port that doesn't support ARC
That's the bridge: it tells the TV it's a sound system, which is how it gets the volume buttons. Dismiss the message and leave the bridge where it is; don't move it to the ARC port.

### Moved the bridge to a different HDMI port
Restart it (admin panel → **Restart Service**) so it picks up its new input. While running, the bridge holds its HDMI connection so a TV waking up doesn't see it drop off and reappear.

### Need to reset to setup mode
Create the force flag and reboot:
```bash
sudo touch /boot/firmware/FORCE_AP_MODE
sudo reboot
```

### Factory reset
From the admin panel at `sonosbridge.local`, go to Settings → Factory Reset

Or via SSH:
```bash
sudo rm /opt/cec-sonos-bridge/config.json
sudo reboot
```

---

## TV Compatibility

Works with any TV that supports HDMI-CEC:

| Brand | CEC Name |
|-------|----------|
| Samsung | Anynet+ |
| LG | SimpLink (use [LG mode](#lg-tvs)) |
| Sony | BRAVIA Sync |
| Vizio | CEC |
| TCL/Roku | CEC |

**Tip:** Avoid plugging into the ARC/eARC HDMI port — use HDMI 1 or 2 instead.

---

## LG TVs

An LG TV only sends its remote's volume buttons over HDMI to a sound system on its ARC port, and a Sonos connected to the TV by optical cable would go silent if the bridge were there. So for LG TVs the bridge uses **LG mode**: it follows the TV's own volume over Wi-Fi and sets your Sonos to the same number. Nothing depends on infrared reaching the Sonos.

### Setting it up

1. Plug the bridge into a **non-ARC** HDMI port on the TV (any port not labelled ARC or eARC).
2. On the LG TV, set **Settings → Sound → Sound Out** to **Optical** (or whatever cable your Sonos uses).
3. If you ever set up the Sonos as a soundbar on the TV (under **Device Connector** or **Universal Control**), remove it there.
4. Open the admin panel (**http://sonosbridge.local**) and tap the **LG TV** tab.
5. Tap **Find my TV**. Your TV should appear in the list; tap it. (If it doesn't, type the TV's IP address in the box. You'll find it on the TV under **Settings → Network → Wi-Fi Connection → Advanced Wi-Fi Settings**, or **Wired Connection**.)
6. Tap **Pair with TV**, then look at the TV and choose **Allow**.
7. Turn on **Follow LG TV volume**.
8. Press volume on the LG remote. The LG TV tab shows something like **TV 23 → Sonos 23**, and your Sonos changes.

### Apple TV

On the Apple TV, go to **Settings → Remotes and Devices → Volume Control** and choose **TV via IR**. The Apple TV remote then changes the TV's volume, and the bridge follows the TV.

### If it doesn't work

The LG TV tab shows whether the bridge is connected to the TV, the volume on both, the TV's Sound Out, and a hint when something needs changing:

- **"Your TV is sending sound over HDMI ARC..."** or **"...playing sound through its own speakers..."** — set **Settings → Sound → Sound Out** to **Optical**.
- **"The TV refused the connection"**, or pairing keeps failing — on the TV, turn on **LG Connect Apps** (newer TVs call it **TV On With Mobile**; look under **Settings → General** or **Settings → Network**).
- **"Can't reach the TV"** — the TV is off, or not on the same Wi-Fi as the bridge. The bridge keeps trying, and reconnects by itself when the TV comes back on.
- **"The TV has forgotten the bridge"** — tap **Pair with TV** again and choose **Allow** on the TV.

If the TV's IP address changes (after a router restart, say), tap **Find my TV** and **Pair with TV** again. Giving the TV a fixed IP address in your router's settings avoids this.

LG mode is off unless you turn it on, so other TVs are unaffected.

---

## How It Works (Technical)

1. **startup.py** — Runs at boot, decides whether to enter AP mode or bridge mode
2. **ap_mode.py** — Creates WiFi hotspot and serves the setup wizard
3. **cec_bridge.py** — Joins HDMI-CEC as an audio system through the kernel's CEC device (`/dev/cec0`), calls Sonos API via `soco` library. It never claims the TV input, so other devices (Fire TV, Apple TV) keep control of what's on screen, and it holds its HDMI connection so a TV waking up never sees it "switch on" (Samsung TVs switch to devices that do). Falls back to `cec-client` if `/dev/cec0` is missing.
4. **web_server.py** — Serves the admin panel at port 80, including the CEC Activity page (`/cec`), which reads the bridge's CEC activity log (`/var/log/cec-sonos-bridge-cec.log`), and the LG TV tab
6. **LG mode** (in `cec_bridge.py`) — Follows an LG webOS TV's own volume and mute over its local WebSocket API ("second screen", `wss://<tv>:3001` or `ws://<tv>:3000`) and sets the Sonos to match. Pairing settings are stored under `lg_tv` in `/opt/cec-sonos-bridge/config.json`. The pairing request is taken from [aiowebostv](https://github.com/home-assistant-libs/aiowebostv) (Apache 2.0).
5. **splash_screen.py** — Generates and displays TV splash screen with QR code (the bridge turns the picture on only while the TV shows its input)

CEC Commands intercepted:
- `05:44:41` → Volume Up
- `05:44:42` → Volume Down  
- `05:44:43` → Mute Toggle

---

## License

MIT License — feel free to use, modify, and share!

---

## Credits

- [SoCo](https://github.com/SoCo/SoCo) — Python library for Sonos control
- [libCEC](https://github.com/Pulse-Eight/libcec) — CEC communication library

---

## Contributing

Found a bug? Have an idea? Open an issue or submit a pull request!

Run the tests (no Pi needed) with `python3 -m unittest discover tests`.
