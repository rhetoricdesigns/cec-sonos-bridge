# CEC-Sonos Bridge 🔊📺

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

- 📱 **Phone-friendly setup wizard** — No coding required
- 📡 **Auto hotspot mode** — Creates WiFi network if it can't connect
- 🖥️ **Admin panel** — Access at `sonosbridge.local`
- 📺 **TV splash screen** — Shows QR code to admin panel
- 🔄 **Auto-recovery** — Falls back to setup mode if WiFi is lost
- ⬆️ **OTA updates** — Update from the admin panel

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
   git clone https://github.com/YOUR_USERNAME/cec-sonos-bridge.git
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
| LG | SimpLink |
| Sony | BRAVIA Sync |
| Vizio | CEC |
| TCL/Roku | CEC |

**Tip:** Avoid plugging into the ARC/eARC HDMI port — use HDMI 1 or 2 instead.

---

## How It Works (Technical)

1. **startup.py** — Runs at boot, decides whether to enter AP mode or bridge mode
2. **ap_mode.py** — Creates WiFi hotspot and serves the setup wizard
3. **cec_bridge.py** — Uses `cec-client` to monitor HDMI-CEC traffic, calls Sonos API via `soco` library
4. **web_server.py** — Serves the admin panel at port 80
5. **splash_screen.py** — Generates and displays TV splash screen with QR code

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
