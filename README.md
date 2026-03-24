# ParkPi — Smart Parking System

Full Raspberry Pi smart parking system with:
- HC-SR04 ultrasonic sensors (real-time occupancy)
- OpenCV + Tesseract licence plate recognition
- Servo barrier gate (auto-opens on valid reservation)
- LED indicators per spot
- Flask + WebSocket live dashboard
- SQLite database with full audit log

---

## Project structure

```
parkpi/
├── app_full.py                  ← main entry point (use this)
├── app.py                       ← simpler version (no plate recognition)
├── schema.sql                   ← database schema
├── requirements.txt
├── parkpi.service               ← systemd autostart
├── plate_recognition/
│   ├── __init__.py
│   └── recognizer.py            ← OpenCV + Tesseract pipeline
└── templates/
    └── index.html               ← web dashboard (served by Flask)
```

---

## Raspberry Pi setup

### 1. System dependencies
```bash
sudo apt update
sudo apt install -y \
  tesseract-ocr tesseract-ocr-eng \
  libgl1 libglib2.0-0 \
  python3-pip git
```

### 2. Clone and install
```bash
git clone https://github.com/your-org/parkpi.git
cd parkpi
pip install -r requirements.txt --break-system-packages
```
Uncomment `picamera2` and `RPi.GPIO` lines in requirements.txt if on real Pi hardware.

### 3. Enable Pi Camera (if using picamera2)
```bash
sudo raspi-config
# → Interface Options → Camera → Enable
```

### 4. Run
```bash
python3 app_full.py
# Dashboard: http://localhost:5000
# Or from any device: http://<pi-ip>:5000
```

### 5. Auto-start on boot
```bash
sudo cp parkpi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable parkpi
sudo systemctl start parkpi
sudo journalctl -u parkpi -f   # live logs
```

---

## Wiring summary

| Component       | Pi GPIO (BCM) | Notes                          |
|----------------|---------------|--------------------------------|
| HC-SR04 VCC    | 5V (pin 2)    | One per spot                   |
| HC-SR04 TRIG   | GPIO4, 27...  | See SPOT_PINS in app_full.py   |
| HC-SR04 ECHO   | GPIO17, 22... | Via 1kΩ resistor (3.3V limit)  |
| HC-SR04 GND    | GND (pin 6)   |                                |
| LED Green      | GPIO2, 14...  | Via 330Ω resistor              |
| LED Red        | GPIO3, 15...  | Via 330Ω resistor              |
| Servo SIG      | GPIO18 (PWM)  | Hardware PWM required          |
| Servo 5V       | 5V            | Use external PSU for MG996R    |
| Servo GND      | GND           |                                |

---

## REST API

| Method | Endpoint             | Description              |
|--------|----------------------|--------------------------|
| GET    | /api/spots           | All spots + reservations |
| GET    | /api/stats           | Occupancy summary        |
| POST   | /api/reserve         | Create reservation       |
| DELETE | /api/reserve/:spotId | Cancel reservation       |
| GET    | /api/reservations    | List all reservations    |
| GET    | /api/plate-log       | Gate access log          |
| POST   | /api/barrier/open    | Manually open gate       |
| WS     | /ws                  | Live state updates       |

---

## Plate recognition pipeline

```
Camera frame
    │
    ▼
Greyscale + bilateral filter
    │
    ▼
Adaptive threshold + dilate
    │
    ▼
Find contours → filter by aspect ratio (1.8–6.5)
    │
    ▼
Upscale candidate regions
    │
    ▼
Tesseract OCR (alphanumeric only)
    │
    ▼
Regex validation → debounce (3 consistent reads)
    │
    ▼
on_plate_detected() callback
    │
    ├── Match found → open barrier + log "granted"
    └── No match   → log "denied"
```

---

## Simulation mode

Running on a laptop without hardware? Everything falls back gracefully:
- `ON_PI = False` → sensor distances are simulated
- No `picamera2` or `RPi.GPIO` → `SimulatedRecognizer` fires fake plates every 15s
- Dashboard fully functional via browser

---

## Extending

- **MQTT**: publish spot state changes to an MQTT broker for multi-lot setups
- **Mobile app**: the REST API is already JSON — point any app at it
- **ANPR cloud**: swap `recognizer.py` with an API call to Plate Recognizer or OpenALPR
- **Payment**: add a `payments` table and a Stripe webhook endpoint
