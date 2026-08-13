# pulsewave.py — M5StickS3 streams a PulseSensor waveform to Chrome over BLE.
#
# Sampling is driven by a hardware timer, not the main loop, so the sample
# interval does not wobble with whatever else the board is doing. The timer ISR
# only writes into a preallocated ring buffer; batching and BLE happen in the
# main loop.
#
# Every packet carries the absolute index of its first sample, so the browser
# can reconstruct exact timing and see precisely how many samples (if any) were
# ever lost.
#
#   packet = <uint32 first_index><uint16 count><uint16 rate_hz><uint32 t_us>
#            then count x uint16 samples (12-bit ADC counts, little-endian)
#
# t_us is ticks_us of the first sample in the packet. MicroPython ticks wrap at
# 2**30, so a receiver must unwrap it (about every 18 minutes).
#
# Streaming starts by itself the moment a central connects -- nothing to press.
import array
import bluetooth
import struct
import time

import M5
import machine
from machine import ADC, Pin, Timer

_UID = machine.unique_id()
NAME = "PulseWave-%02X%02X" % (_UID[4], _UID[5])

SENSOR_GPIO = 2
# 250 Hz, not the PulseSensor reference 500 Hz, and that is deliberate: at 2 ms
# the soft-IRQ timer cannot keep up once BLE is busy and the measured rate sags
# to ~493 Hz with the period creeping to 2028 us. At 4 ms the mean interval is
# exactly 4000 us with zero dropped samples. A pulse waveform's useful content
# sits below ~25 Hz, so this is still an order of magnitude of oversampling.
RATE = 250
CHUNK = 16                 # samples per BLE packet -> 64 ms of latency
RING = 1024                # must stay a power of two (index masking)
_MASK = RING - 1

_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3
_IRQ_MTU_EXCHANGED = 21

_F_READ = 0x0002
_F_WRITE_NR = 0x0004
_F_WRITE = 0x0008
_F_NOTIFY = 0x0010

_UART_UUID = bluetooth.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
_TX = (bluetooth.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E"), _F_READ | _F_NOTIFY)
_RX = (bluetooth.UUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E"), _F_WRITE | _F_WRITE_NR)
_SERVICE = (_UART_UUID, (_TX, _RX))

# --- acquisition ------------------------------------------------------------
_adc = ADC(Pin(SENSOR_GPIO))
_adc.atten(ADC.ATTN_11DB)          # full 0-3.3 V swing
_buf = array.array("H", bytearray(2 * RING))
_ts = array.array("I", bytearray(4 * RING))     # ticks_us per sample
_w = 0                             # total samples ever written (monotonic)
_clock = [0, 0]                    # [local epoch seconds from the browser, ticks_ms when set]


def _tick(_t):
    # ISR: no allocation. Stamping every sample costs one extra store and lets
    # the receiver measure the true rate and jitter instead of trusting RATE.
    global _w
    i = _w & _MASK
    _buf[i] = _adc.read_u16() >> 4              # 16-bit reading -> 12-bit counts
    _ts[i] = time.ticks_us()
    _w += 1


def _payload(flags=False, name=None, services=None):
    p = bytearray()

    def add(t, v):
        p.extend(struct.pack("BB", len(v) + 1, t) + v)

    if flags:
        add(0x01, struct.pack("B", 0x06))
    if name:
        add(0x09, name)
    for u in services or ():
        b = bytes(u)
        # A 128-bit UUID needs 18 bytes; with the name that would overflow the
        # 31-byte advertisement, so it rides in the scan response.
        add(0x07 if len(b) == 16 else 0x03, b)
    return p


class WaveLink:
    def __init__(self):
        self._conns = set()
        self._need_adv = False
        self.streaming = False
        self.dropped = 0
        self.sent = 0
        self.ble = bluetooth.BLE()
        self.ble.active(True)
        self.ble.config(gap_name=NAME)
        try:
            self.ble.config(mtu=247)
        except Exception:
            pass
        ((self._tx, self._rx),) = self.ble.gatts_register_services((_SERVICE,))
        self.ble.irq(self._irq)
        self._advertise()

    def _advertise(self):
        self.ble.gap_advertise(
            100_000,
            adv_data=_payload(flags=True, name=NAME),
            resp_data=_payload(services=[_UART_UUID]),
        )

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            conn, _, _ = data
            self._conns.add(conn)
            self.streaming = True          # low friction: no start button
            print("BLE: connected", conn, "- streaming")
        elif event == _IRQ_CENTRAL_DISCONNECT:
            conn, _, _ = data
            self._conns.discard(conn)
            if not self._conns:
                self.streaming = False
            print("BLE: disconnected", conn)
            # Re-advertising from inside the IRQ raises OSError -30 when the
            # stack is still tearing the link down, which would leave the stick
            # invisible and unreconnectable. Defer it to the main loop.
            self._need_adv = True
        elif event == _IRQ_MTU_EXCHANGED:
            # A 44-byte notification (12-byte header + 16 x uint16 samples)
            # needs an ATT MTU >= 47. The unnegotiated default is 23, which
            # would silently truncate every packet -- this print is the only
            # way to see that actually happened on real hardware.
            conn, mtu = data
            print("BLE: MTU exchanged ->", mtu, "usable payload", mtu - 3)
        elif event == _IRQ_GATTS_WRITE:
            conn, handle = data
            if handle != self._rx:
                return
            cmd = self.ble.gatts_read(self._rx)
            head = cmd[:1]
            if head == b"S":
                self.streaming = True
            elif head == b"X":
                self.streaming = False
            elif head == b"T":
                # Wall-clock handed over by the browser, as local seconds since
                # the unix epoch. Kept as an offset instead of setting the RTC.
                try:
                    _clock[0] = int(cmd[1:])
                    _clock[1] = time.ticks_ms()
                except ValueError:
                    pass

    @property
    def connected(self):
        return bool(self._conns)

    def service(self):
        """Main-loop chores that must not run in interrupt context."""
        if self._need_adv and not self._conns:
            try:
                self._advertise()
                self._need_adv = False
                print("BLE: advertising again")
            except OSError:
                pass          # stack still busy; retry on the next pass

    def notify(self, payload):
        for c in tuple(self._conns):
            try:
                self.ble.gatts_notify(c, self._tx, payload)
            except OSError:
                return False
        return True


def main():
    global _t_first
    M5.begin()
    lcd = M5.Lcd
    lcd.setRotation(1)
    W, H = lcd.width(), lcd.height()

    link = WaveLink()

    # Screen layout, measured once. 240x135 at rotation 1.
    WAVE_X, WAVE_Y, WAVE_W, WAVE_H = 4, 20, W - 8, 44
    STATUS_Y = 70
    BAR_Y = 96
    FOOT_Y = 106

    _t_first = time.ticks_us()
    Timer(0).init(period=1000 // RATE, mode=Timer.PERIODIC, callback=_tick)
    print("PulseWave up:", NAME, "- sampling GPIO", SENSOR_GPIO, "at", RATE, "Hz")

    r = 0                       # next sample index to transmit
    shown = None
    flash = [-10000]                 # ticks_ms of the last "cleared" confirmation
    last_ui = time.ticks_ms()
    pkt = bytearray(12 + 2 * CHUNK)

    while True:
        w = _w
        if link.streaming and (w - r) >= CHUNK:
            # If the link stalled long enough for the ring to lap us, skip to
            # the newest complete window and count exactly what was lost.
            if (w - r) > RING:
                lost = (w - r) - RING
                link.dropped += lost
                r = w - RING
                print("PulseWave: dropped", lost, "samples")
            struct.pack_into("<IHHI", pkt, 0, r, CHUNK, RATE, _ts[r & _MASK])
            o = 12
            for i in range(CHUNK):
                v = _buf[(r + i) & _MASK]
                pkt[o] = v & 0xFF
                pkt[o + 1] = v >> 8
                o += 2
            if link.notify(pkt):
                link.sent += 1
                r += CHUNK
        elif not link.streaming:
            r = _w              # idle: stay at the live edge, never backlog

        link.service()

        # BtnA / BtnB wipe the browser's drawings and restart on fresh data.
        M5.update()
        if M5.BtnA.wasPressed() or M5.BtnB.wasPressed():
            if link.notify(b"CLR!"):
                flash[0] = time.ticks_ms()
            shown = None                       # force a repaint

        now = time.ticks_ms()
        if time.ticks_diff(now, last_ui) > 200:
            last_ui = now
            # Peak-to-peak says whether a finger is really on the sensor -- a
            # connected link with a flat trace is the failure people actually
            # hit, so it gets its own state on screen. Scanned every other
            # sample: at 250 Hz that is plenty, and it halves the repaint cost.
            n = min(3 * RATE, _w)      # ~3 s: the review window and the pulse test
            lo, hi = 4096, 0
            for k in range(0, n, 2):
                v = _buf[(_w - 1 - k) & _MASK]
                if v < lo:
                    lo = v
                if v > hi:
                    hi = v
            pp = hi - lo if n else 0

            if time.ticks_diff(now, flash[0]) < 900:
                word, col = "CLEARED", 0x00CCFF
            elif not link.connected:
                word, col = "WAITING", 0xFFAA00
            elif pp < 25:
                word, col = "NO PULSE", 0xFF5555
            else:
                word, col = "LIVE", 0x00FF66

            # Repaint the fixed furniture only when the state word changes;
            # everything else is small fills, so the screen never flickers.
            if word != shown:
                shown = word
                lcd.fillScreen(0x000000)
                lcd.drawRect(WAVE_X, WAVE_Y, WAVE_W, WAVE_H, 0x223344)
                lcd.setTextSize(2)
                lcd.setTextColor(col, 0x000000)
                lcd.setCursor((W - lcd.textWidth(word)) // 2, STATUS_Y)
                lcd.print(word)

            # --- header: link state, name, battery -------------------------
            lcd.setTextSize(1)
            lcd.fillRect(0, 0, W, 16, 0x000000)
            lcd.setTextColor(0x00FF66 if link.connected else 0x556677, 0x000000)
            lcd.setCursor(4, 4)
            lcd.print("BLE" if link.connected else "---")
            lcd.setTextColor(0x8899AA, 0x000000)
            short = NAME[-4:]
            lcd.setCursor((W - lcd.textWidth(short)) // 2, 4)
            lcd.print(short)
            try:
                batt = M5.Power.getBatteryLevel()
                charging = M5.Power.isCharging()
            except Exception:
                batt, charging = -1, False
            if batt >= 0:
                bx = W - 34
                lcd.drawRect(bx, 3, 26, 12, 0x8899AA)
                lcd.fillRect(bx + 26, 6, 2, 6, 0x8899AA)
                bcol = 0x00CCFF if charging else (0x00FF66 if batt > 40 else
                                                 0xFFAA00 if batt > 15 else 0xFF5555)
                lcd.fillRect(bx + 2, 5, (22 * batt) // 100, 8, bcol)
                lcd.fillRect(bx + 2 + (22 * batt) // 100, 5,
                             22 - (22 * batt) // 100, 8, 0x000000)

            # --- mini waveform review window -------------------------------
            # One filled column per 2 px, spanning the min..max of the samples
            # that fall in it, so a fast upstroke keeps its full height.
            lcd.fillRect(WAVE_X + 1, WAVE_Y + 1, WAVE_W - 2, WAVE_H - 2, 0x000000)
            if n > 8:
                span = hi - lo
                if span < 20:
                    span = 20
                cols = (WAVE_W - 2) // 2
                per = n // cols
                if per < 1:
                    per = 1
                inner = WAVE_H - 4
                for cix in range(cols):
                    cl, ch = 4096, 0
                    base = _w - n + cix * per
                    for j in range(0, per, 2):
                        v = _buf[(base + j) & _MASK]
                        if v < cl:
                            cl = v
                        if v > ch:
                            ch = v
                    if ch < cl:
                        continue
                    y1 = WAVE_Y + 2 + inner - (ch - lo) * inner // span
                    y2 = WAVE_Y + 2 + inner - (cl - lo) * inner // span
                    lcd.fillRect(WAVE_X + 1 + cix * 2, y1, 2, (y2 - y1) or 1, col)

            # --- footer: uptime, clock, signal bar -------------------------
            fill = pp if pp < 900 else 900
            fill = (W - 8) * fill // 900
            lcd.fillRect(4, BAR_Y, fill, 5, col)
            lcd.fillRect(4 + fill, BAR_Y, (W - 8) - fill, 5, 0x111820)

            up = time.ticks_ms() // 1000
            lcd.fillRect(0, FOOT_Y, W, 14, 0x000000)
            lcd.setTextColor(0x8899AA, 0x000000)
            lcd.setCursor(4, FOOT_Y)
            lcd.print("up %d:%02d:%02d" % (up // 3600, (up // 60) % 60, up % 60))
            if _clock[0]:
                secs = _clock[0] + time.ticks_diff(now, _clock[1]) // 1000
                stamp = "%02d:%02d:%02d" % ((secs // 3600) % 24, (secs // 60) % 60, secs % 60)
            else:
                stamp = "--:--:--"
            lcd.setCursor(W - lcd.textWidth(stamp) - 4, FOOT_Y)
            lcd.print(stamp)

        # Unconditional yield: a frame that never sleeps starves the task
        # watchdog and the board reboot-loops.
        time.sleep_ms(2)


main()
