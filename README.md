> **Personal independent demo by Yury Gitman.** This repository is not official World Famous Electronics or PulseSensor documentation and does not define current product compatibility or support policy. Current WFE information lives under [`WorldFamousElectronics`](https://github.com/WorldFamousElectronics).

# StickS3 &rarr; BLE &rarr; Chrome: Live 3D FFT

A minimal, modular demo: an M5Stack StickS3 samples a signal and relays it
over Bluetooth LE, completely unprocessed. Chrome does every bit of the math
(FFT) and all the drawing -- a live, orbit-able 3D isometric terrain of the
signal's frequency content, rebuilt roughly 7 times a second.

One button. One 3D view. Nothing else.

This is the visualization from the [GameEngine / PPGFFTGAMEENGINE](https://github.com/yury-g/PPGFFTGAMEENGINE)
sibling project, ported to run over Bluetooth instead of USB Serial, and with
every PPG-specific reading, label, and control stripped out -- just the
terrain itself. The stick firmware is a copy of `pulsewave.py` from the
[MStackSTICK-S3 / PulseLink](https://github.com/yury-g/pulsesensor-m5sticks3)
project's wiring and BLE relay, reused unmodified.

## Why this exists

The stick and the browser are two interchangeable, general-purpose modules.
The stick's whole job is: sample a GPIO pin at a steady rate and stream the
raw numbers over BLE. It has no idea what the signal means, and doesn't need
to -- that's deliberate. The browser's whole job is: take any BLE stream in
that same simple format and turn it into a live 3D frequency-domain
visualization. Point the exact same stick firmware at a different sensor and
this page draws its FFT with zero code changes.

## Hardware

| Quantity | Component | Purpose |
|---:|---|---|
| 1 | M5Stack StickS3 | Samples the signal and relays it over BLE |
| 1 | PulseSensor kit (or any analog sensor on GPIO2) | Provides a signal with visible frequency content |

### Wiring

Disconnect USB power before wiring.

| PulseSensor lead | StickS3 |
|---|---|
| Signal / purple | **G2 / GPIO2** |
| VCC / red | **3V3** |
| GND / black | **GND** |

Power the sensor from **3.3 V only**. Do not connect it to 5 V.

## Software quick start

**Stick** -- UIFlow2 v2.4.9 MicroPython, one file:

1. Install or restore UIFlow2 v2.4.9 with M5Burner.
2. Install `mpremote`: `python3 -m pip install mpremote`
3. Clone this repo and deploy the relay firmware as `main.py`:
   ```bash
   git clone https://github.com/yury-g/StickS3-BLE-Chrome-3D-FFT.git
   cd StickS3-BLE-Chrome-3D-FFT
   python3 -m mpremote connect auto fs cp sticks3-relay.py :main.py
   ```
4. Reset the StickS3. It starts advertising over BLE immediately -- no
   button press needed.

**Browser** -- one static file, no build step:

```bash
python3 -m http.server 8127
```

Open `http://localhost:8127/index.html` in desktop Chrome (Web Bluetooth
requires a secure context; `localhost` qualifies) and click **Connect
StickS3**.

## Using it

- **Drag** the 3D view to orbit.
- **Scroll** to zoom.
- **Reset view** returns to the default angle.

The terrain colors are a fixed palette by frequency band (amber under
~0.6 Hz, teal 0.7-3.2 Hz, violet above) -- purely a visual signature carried
over from the GameEngine sibling project, not a claim about what any
particular peak means. This page draws; it doesn't interpret. For frequency
analysis, harmonic detection, or a plain-language read of what a peak might
be, see the
[Pulsewave Harmonics](https://github.com/yury-g/PulsewaveHarmonics) sibling
project.

## BLE wire format

The stick just relays; this is the entire protocol.

```
<uint32 first_index><uint16 count><uint16 rate_hz><uint32 t_us>
then count x uint16 little-endian samples (12-bit ADC counts)
```

Nordic UART Service, notify-only (the browser never writes to the stick):

- Service: `6e400001-b5a3-f393-e0a9-e50e24dcca9e`
- TX (notify): `6e400003-b5a3-f393-e0a9-e50e24dcca9e`

## Credits

Terrain visualization ported from
[PPGFFTGAMEENGINE](https://github.com/yury-g/PPGFFTGAMEENGINE). Stick
wiring and BLE relay firmware from
[pulsesensor-m5sticks3 / PulseLink](https://github.com/yury-g/pulsesensor-m5sticks3).
