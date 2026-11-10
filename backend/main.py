
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from ai_engine import router as ai_router

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from fastapi.responses import FileResponse

import serial
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "test_history.db"

# Set ARDUINO_PORT to the COM port shown by Arduino IDE.
ARDUINO_PORT = os.getenv("ARDUINO_PORT", "COM3")
BAUD_RATE = 115200
SERIAL_TIMEOUT_SECONDS = 3.0
SERIES_RESISTOR_OHM = 330.0

# Initial acceptance limits for a red LED and the documented fixture.
# These are starting limits, not calibrated laboratory specifications.
LIMITS = {
    "drive_voltage_min_v": 4.0,
    "forward_voltage_min_v": 1.4,
    "forward_voltage_max_v": 2.6,
    "led_current_min_ma": 2.0,
    "led_current_max_ma": 20.0,
    "open_voltage_min_v": 4.0,
    "open_current_max_ma": 0.5,
    "short_voltage_max_v": 0.5,
    "short_current_min_ma": 2.0,
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def init_database():
    with sqlite3.connect(DATABASE) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS test_runs (
                run_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                component TEXT NOT NULL,
                overall_status TEXT NOT NULL,
                measurements_json TEXT NOT NULL,
                results_json TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS test_suites (
                suite_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                component TEXT NOT NULL,
                device_status TEXT NOT NULL,
                suite_status TEXT NOT NULL,
                report_json TEXT NOT NULL
            )
        """)
        db.commit()


class ArduinoClient:
    """Serialized USB serial access to the Arduino firmware."""

    def __init__(self):
        self.connection = None
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            if self.connection is not None:
                try:
                    self.connection.close()
                except serial.SerialException:
                    pass
                self.connection = None

    def _open_if_needed(self):
        if self.connection is not None and self.connection.is_open:
            return

        self.connection = None
        self.connection = serial.Serial(
            port=ARDUINO_PORT,
            baudrate=BAUD_RATE,
            timeout=0.25,
            write_timeout=SERIAL_TIMEOUT_SECONDS,
        )

        # Opening a UNO serial connection may reset the board.
        time.sleep(2.0)
        self.connection.reset_input_buffer()
        self.connection.reset_output_buffer()

    def _request_locked(self, command, timeout=SERIAL_TIMEOUT_SECONDS):
        self._open_if_needed()

        message = json.dumps({"cmd": command}) + "\n"
        self.connection.write(message.encode("utf-8"))
        self.connection.flush()

        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            raw = self.connection.readline()

            if not raw:
                continue

            try:
                response = json.loads(raw.decode("utf-8").strip())
            except (UnicodeDecodeError, json.JSONDecodeError):
                # Ignore startup noise; only accept valid JSON objects.
                continue

            if not isinstance(response, dict):
                continue

            if response.get("event") == "ready":
                continue

            return response

        raise TimeoutError("Arduino did not return a valid JSON response.")

    def request(self, command):
        # Only allow commands that the current firmware supports.
        if command not in {"ping", "test_led"}:
            raise ValueError("Unsupported Arduino command.")

        with self.lock:
            try:
                return self._request_locked(command)
            except (serial.SerialException, OSError, TimeoutError):
                if self.connection is not None:
                    try:
                        self.connection.close()
                    except serial.SerialException:
                        pass
                self.connection = None
                raise

    def status(self):
        response = self.request("ping")
        if response.get("ok") is not True or response.get("type") != "pong":
            raise RuntimeError("Unexpected Arduino ping response.")
        return response


arduino = ArduinoClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    yield
    arduino.close()


app = FastAPI(
    title="AI Embedded Test Engineer API",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(ai_router)

@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(BASE_DIR / "static" / "index.html")

# Dashboard and API run on the same local origin in the next phase.
# These origins also support local development servers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class LEDTestRequest(BaseModel):
    component_label: str = Field(default="LED A", min_length=1, max_length=40)
class SuiteRequest(BaseModel):
    component_label: str = Field(
        default="LED A",
        min_length=1,
        max_length=40,
    )

def validate_measurements(data):
    required = [
        "adc_a0",
        "adc_a1",
        "voltage_a0_v",
        "voltage_a1_v",
        "series_current_ma",
        "led_current_ma",
    ]

    for key in required:
        if key not in data:
            raise ValueError(f"Missing measurement: {key}")

        value = data[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Invalid numeric measurement: {key}")

        if not math.isfinite(value):
            raise ValueError(f"Non-finite measurement: {key}")

    if not (0 <= data["adc_a0"] <= 1023):
        raise ValueError("ADC A0 reading is outside the 10-bit range.")

    if not (0 <= data["adc_a1"] <= 1023):
        raise ValueError("ADC A1 reading is outside the 10-bit range.")

    if not (0 <= data["voltage_a0_v"] <= 5.2):
        raise ValueError("A0 voltage is outside the allowed range.")

    if not (0 <= data["voltage_a1_v"] <= 5.2):
        raise ValueError("A1 voltage is outside the allowed range.")

    if not (0 <= data["series_current_ma"] <= 25):
        raise ValueError("Series current is outside the fixture's expected range.")

    if not (0 <= data["led_current_ma"] <= 25):
        raise ValueError("LED current is outside the fixture's expected range.")


def evaluate_led(data):
    """Apply deterministic criteria. Never let AI determine the verdict."""
    validate_measurements(data)

    a0 = data["voltage_a0_v"]
    a1 = data["voltage_a1_v"]
    led_current = data["led_current_ma"]
    series_current = data["series_current_ma"]

    open_like = (
        a1 >= LIMITS["open_voltage_min_v"]
        and led_current <= LIMITS["open_current_max_ma"]
    )
    short_like = (
        a1 <= LIMITS["short_voltage_max_v"]
        and series_current >= LIMITS["short_current_min_ma"]
    )

    tests = []

    def add(test_id, purpose, expected, actual, passed, unit=None):
        tests.append({
            "test_id": test_id,
            "purpose": purpose,
            "expected": expected,
            "actual": actual,
            "unit": unit,
            "status": "PASS" if passed else "FAIL",
        })

    add(
        "LED-001",
        "Check drive-side voltage",
        f">= {LIMITS['drive_voltage_min_v']} V",
        round(a0, 4),
        a0 >= LIMITS["drive_voltage_min_v"],
        "V",
    )

    add(
        "LED-002",
        "Check LED forward voltage for a red LED",
        f"{LIMITS['forward_voltage_min_v']} to "
        f"{LIMITS['forward_voltage_max_v']} V",
        round(a1, 4),
        LIMITS["forward_voltage_min_v"] <= a1
        <= LIMITS["forward_voltage_max_v"],
        "V",
    )

    add(
        "LED-003",
        "Check LED current",
        f"{LIMITS['led_current_min_ma']} to "
        f"{LIMITS['led_current_max_ma']} mA",
        round(led_current, 4),
        LIMITS["led_current_min_ma"] <= led_current
        <= LIMITS["led_current_max_ma"],
        "mA",
    )

    add(
        "LED-004",
        "Check for open-circuit-like behavior",
        "No open-like behavior",
        "Open-like behavior detected" if open_like else "Not detected",
        not open_like,
    )

    add(
        "LED-005",
        "Check for short-circuit-like behavior",
        "No short-like behavior",
        "Short-like behavior detected" if short_like else "Not detected",
        not short_like,
    )

    if any(test["status"] == "FAIL" for test in tests):
        overall = "FAIL"
    elif any(test["status"] == "INCONCLUSIVE" for test in tests):
        overall = "INCONCLUSIVE"
    else:
        overall = "PASS"

    if open_like:
        diagnosis = (
            "Open-circuit-like behavior. Check LED polarity, seating, "
            "connections, and the LED itself."
        )
    elif short_like:
        diagnosis = (
            "Short-circuit-like behavior. Inspect the LED and breadboard "
            "for a bridge or wiring error."
        )
    elif overall == "PASS":
        diagnosis = (
            "The measurements meet the initial red-LED acceptance limits. "
            "This does not rule out every possible LED fault."
        )
    else:
        diagnosis = (
            "One or more measurements are outside the initial limits. "
            "Inspect the fixture and compare with a known-good LED."
        )

    return {
        "overall_status": overall,
        "tests": tests,
        "diagnosis": diagnosis,
    }


def save_run(run_id, timestamp, component, overall, measurements, results):
    with sqlite3.connect(DATABASE) as db:
        db.execute(
            """
            INSERT INTO test_runs
            (run_id, timestamp, component, overall_status,
             measurements_json, results_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                timestamp,
                component,
                overall,
                json.dumps(measurements),
                json.dumps(results),
            ),
        )
        db.commit()


@app.get("/api/status")
def get_status():
    try:
        response = arduino.status()
        return {
            "connected": True,
            "port": ARDUINO_PORT,
            "board": response.get("board", "Arduino UNO R3"),
            "firmware": response.get("firmware"),
        }
    except (serial.SerialException, OSError, TimeoutError, RuntimeError) as exc:
        return {
            "connected": False,
            "port": ARDUINO_PORT,
            "error": str(exc),
        }


@app.post("/api/test/led")
def run_led_test(request: LEDTestRequest):
    try:
        response = arduino.request("test_led")
    except (serial.SerialException, OSError, TimeoutError) as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "ERROR",
                "message": (
                    "Arduino communication failed. Check the USB connection "
                    "and COM port. No component fault was recorded."
                ),
                "reason": str(exc),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if response.get("ok") is not True:
        raise HTTPException(
            status_code=502,
            detail={
                "status": "ERROR",
                "message": "Arduino rejected the test command.",
                "arduino_response": response,
            },
        )

    if response.get("type") != "led_measurement":
        raise HTTPException(
            status_code=502,
            detail="Unexpected Arduino response type.",
        )

    try:
        measurements = {
            key: response[key]
            for key in (
                "adc_a0",
                "adc_a1",
                "voltage_a0_v",
                "voltage_a1_v",
                "series_current_ma",
                "led_current_ma",
            )
        }
        evaluation = evaluate_led(measurements)
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "status": "INCONCLUSIVE",
                "message": "The returned measurements are invalid.",
                "reason": str(exc),
            },
        ) from exc

    run_id = str(uuid.uuid4())
    timestamp = utc_now()

    result = {
        "run_id": run_id,
        "timestamp": timestamp,
        "component": request.component_label,
        "overall_status": evaluation["overall_status"],
        "measurements": measurements,
        "tests": evaluation["tests"],
        "diagnosis": evaluation["diagnosis"],
        "firmware_hint": response.get("hint"),
        "limits": LIMITS,
    }

    save_run(
        run_id,
        timestamp,
        request.component_label,
        evaluation["overall_status"],
        measurements,
        result,
    )

    return result


def suite_case(test_id, category, purpose, execution, status,
               expected, actual=None, details=None):
    return {
        "test_id": test_id,
        "category": category,
        "purpose": purpose,
        "execution": execution,
        "status": status,
        "expected": expected,
        "actual": actual,
        "details": details,
    }


@app.post("/api/ai/run-suite")
def run_ai_test_suite(request: SuiteRequest):
    """Run supported checks and save a suite report.

    Only LED-001 performs a physical LED measurement in this version.
    Other cases are software checks, protocol checks, or explicitly blocked
    manual hardware checks.
    """
    label = request.component_label.strip()
    if not label:
        raise HTTPException(status_code=422, detail="Component label is required.")

    suite_id = str(uuid.uuid4())
    timestamp = utc_now()
    cases = []
    physical_report = None

    # LED-001: Perform the actual physical test through the existing path.
    try:
        physical_report = run_led_test(
            LEDTestRequest(component_label=label)
        )
        cases.append(suite_case(
            "LED-001", "normal",
            "Verify normal LED operation using real measurements.",
            "HARDWARE",
            physical_report["overall_status"],
            "All applicable configured LED checks pass.",
            {
                "overall_status": physical_report["overall_status"],
                "measurements": physical_report["measurements"],
                "tests": physical_report["tests"],
            },
            "Real physical measurements were collected and the existing "
            "deterministic evaluator supplied the verdict.",
        ))
    except HTTPException as exc:
        # A serial or API error is not evidence that the LED is faulty.
        detail = exc.detail
        cases.append(suite_case(
            "LED-001", "normal",
            "Verify normal LED operation using real measurements.",
            "HARDWARE",
            "ERROR",
            "A valid physical measurement report is returned.",
            detail,
            "The physical test could not be completed. No component failure "
            "is inferred from a communication or test-system error.",
        ))

    # Software-only cases use synthetic data. They never send fake readings
    # to the Arduino or save synthetic data as a physical run.
    base = {
        "adc_a0": 1023,
        "adc_a1": 409,
        "voltage_a0_v": 5.0,
        "voltage_a1_v": 2.0,
        "series_current_ma": (5.0 - 2.0) / 330.0 * 1000.0,
        "led_current_ma": LIMITS["led_current_min_ma"],
    }

    # LED-002: Test the inclusive lower current boundary.
    try:
        synthetic = dict(base)
        synthetic["led_current_ma"] = LIMITS["led_current_min_ma"]
        evaluation = evaluate_led(synthetic)
        current_test = next(
            test for test in evaluation["tests"]
            if test["test_id"] == "LED-003"
        )
        passed = current_test["status"] == "PASS"
        cases.append(suite_case(
            "LED-002", "boundary",
            "Check the lower accepted current boundary.",
            "SOFTWARE_SIMULATION",
            "PASS" if passed else "FAIL",
            f"Current at lower limit {LIMITS['led_current_min_ma']} mA "
            "is accepted.",
            {
                "synthetic_led_current_ma": synthetic["led_current_ma"],
                "evaluator_status": current_test["status"],
            },
            "This validates the Python boundary rule only; no hardware "
            "measurement was made for this case.",
        ))
    except (ValueError, StopIteration) as exc:
        cases.append(suite_case(
            "LED-002", "boundary", "Check lower current boundary.",
            "SOFTWARE_SIMULATION", "ERROR",
            "Lower boundary is accepted.", str(exc),
        ))

    # LED-003: Test the inclusive upper current boundary.
    try:
        synthetic = dict(base)
        synthetic["led_current_ma"] = LIMITS["led_current_max_ma"]
        evaluation = evaluate_led(synthetic)
        current_test = next(
            test for test in evaluation["tests"]
            if test["test_id"] == "LED-003"
        )
        passed = current_test["status"] == "PASS"
        cases.append(suite_case(
            "LED-003", "boundary",
            "Check the upper accepted current boundary.",
            "SOFTWARE_SIMULATION",
            "PASS" if passed else "FAIL",
            f"Current at upper limit {LIMITS['led_current_max_ma']} mA "
            "is accepted.",
            {
                "synthetic_led_current_ma": synthetic["led_current_ma"],
                "evaluator_status": current_test["status"],
            },
            "This validates the Python boundary rule only; no hardware "
            "measurement was made for this case.",
        ))
    except (ValueError, StopIteration) as exc:
        cases.append(suite_case(
            "LED-003", "boundary", "Check upper current boundary.",
            "SOFTWARE_SIMULATION", "ERROR",
            "Upper boundary is accepted.", str(exc),
        ))

    # LED-004: The current fixture has no remotely controlled fault switch.
    # Do not silently disconnect or alter a physical component.
    cases.append(suite_case(
        "LED-004", "fault_injection",
        "Check open-circuit-like behavior.",
        "MANUAL_HARDWARE_SETUP",
        "BLOCKED",
        "With the approved open-circuit simulation applied, current should "
        "be near zero and the result should be interpreted correctly.",
        None,
        "Apply the documented open-circuit simulation manually, then run "
        "a separate physical LED test. This endpoint cannot actuate the "
        "fixture, so it has not claimed to test this fault.",
    ))

    # LED-005: Ensure an out-of-range ADC reading is rejected.
    try:
        invalid = dict(base)
        invalid["adc_a0"] = 1100
        try:
            validate_measurements(invalid)
            rejected = False
        except ValueError:
            rejected = True

        cases.append(suite_case(
            "LED-005", "invalid_input",
            "Reject an ADC value outside the 10-bit range.",
            "SOFTWARE_SIMULATION",
            "PASS" if rejected else "FAIL",
            "ADC value 1100 is rejected by the validator.",
            {"synthetic_adc_a0": 1100, "rejected": rejected},
            "Software validation only; no command or synthetic reading "
            "was sent to the Arduino.",
        ))
    except Exception as exc:
        cases.append(suite_case(
            "LED-005", "invalid_input", "Reject out-of-range ADC.",
            "SOFTWARE_SIMULATION", "ERROR",
            "Invalid ADC is rejected.", str(exc),
        ))

    # LED-006: Ensure missing and non-finite values are rejected.
    try:
        missing = dict(base)
        missing.pop("led_current_ma")
        nonfinite = dict(base)
        nonfinite["led_current_ma"] = float("nan")

        rejected_missing = False
        rejected_nonfinite = False

        try:
            validate_measurements(missing)
        except ValueError:
            rejected_missing = True

        try:
            validate_measurements(nonfinite)
        except ValueError:
            rejected_nonfinite = True

        passed = rejected_missing and rejected_nonfinite
        cases.append(suite_case(
            "LED-006", "invalid_input",
            "Reject missing and non-finite measurements.",
            "SOFTWARE_SIMULATION",
            "PASS" if passed else "FAIL",
            "Both missing and NaN measurements are rejected.",
            {
                "missing_value_rejected": rejected_missing,
                "nan_value_rejected": rejected_nonfinite,
            },
            "Software validation only; no physical component verdict "
            "is affected.",
        ))
    except Exception as exc:
        cases.append(suite_case(
            "LED-006", "invalid_input",
            "Reject missing and non-finite measurements.",
            "SOFTWARE_SIMULATION", "ERROR",
            "Both invalid reports are rejected.", str(exc),
        ))

    # LED-007: Analyze the real physical report, if available.
    if physical_report is None:
        cases.append(suite_case(
            "LED-007", "unexpected",
            "Check current-calculation consistency.",
            "REAL_MEASUREMENT_REQUIRED", "INCONCLUSIVE",
            "Compare measured voltages with reported series current.",
            None,
            "No valid physical report was available for comparison.",
        ))
    else:
        m = physical_report["measurements"]
        calculated = (
            (m["voltage_a0_v"] - m["voltage_a1_v"])
            / SERIES_RESISTOR_OHM * 1000.0
        )
        reported = m["series_current_ma"]
        difference = abs(calculated - reported)
        tolerance = 0.15
        passed = difference <= tolerance

        cases.append(suite_case(
            "LED-007", "unexpected",
            "Check current-calculation consistency.",
            "REAL_MEASUREMENT_ANALYSIS",
            "PASS" if passed else "FAIL",
            f"Absolute difference <= {tolerance} mA (provisional).",
            {
                "recalculated_series_current_ma": round(calculated, 4),
                "reported_series_current_ma": reported,
                "absolute_difference_ma": round(difference, 4),
                "provisional_tolerance_ma": tolerance,
            },
            "Uses the actual report from LED-001. Calibrate the tolerance "
            "for ADC resolution, resistor tolerance and rounding.",
        ))

    # LED-008: Check ping and the backend's unsupported-command guard.
    try:
        ping = arduino.status()
        ping_ok = (
            ping.get("ok") is True and ping.get("type") == "pong"
        )

        try:
            arduino.request("unsupported_suite_command")
            unsupported_rejected = False
        except ValueError:
            unsupported_rejected = True

        passed = ping_ok and unsupported_rejected
        cases.append(suite_case(
            "LED-008", "regression",
            "Check ping and unsupported-command rejection.",
            "SERIAL_PROTOCOL",
            "PASS" if passed else "FAIL",
            "Ping returns pong and the backend rejects unsupported commands.",
            {
                "ping_valid": ping_ok,
                "backend_rejected_unsupported_command": unsupported_rejected,
            },
            "The unsupported command is rejected by the Python client before "
            "it reaches the Arduino. This does not independently verify "
            "firmware-side rejection.",
        ))
    except (serial.SerialException, OSError, TimeoutError, RuntimeError) as exc:
        cases.append(suite_case(
            "LED-008", "regression",
            "Check ping and unsupported-command rejection.",
            "SERIAL_PROTOCOL", "ERROR",
            "Ping succeeds and unsupported commands are rejected.",
            str(exc),
            "Communication/protocol verification failed; this is not proof "
            "that the component is faulty.",
        ))

    physical_status = (
        physical_report["overall_status"]
        if physical_report is not None
        else "ERROR"
    )

    # The device verdict comes only from the physical evaluator.
    device_status = physical_status

    # The suite itself is inconclusive if a required case is blocked or
    # could not establish a result, even when the physical LED passes.
    if any(case["status"] == "FAIL" for case in cases):
        suite_status = "FAIL"
    elif any(
        case["status"] in {"ERROR", "BLOCKED", "INCONCLUSIVE"}
        for case in cases
    ):
        suite_status = "INCONCLUSIVE"
    else:
        suite_status = "PASS"

    counts = {
        status: sum(1 for case in cases if case["status"] == status)
        for status in ("PASS", "FAIL", "INCONCLUSIVE", "ERROR", "BLOCKED")
    }

    report = {
        "suite_id": suite_id,
        "timestamp": timestamp,
        "component": label,
        "device_status": device_status,
        "suite_status": suite_status,
        "counts": counts,
        "total_cases": len(cases),
        "executed_cases": sum(
            1 for case in cases
            if case["status"] not in {"BLOCKED", "INCONCLUSIVE", "ERROR"}
        ),
        "physical_report": physical_report,
        "cases": cases,
        "summary": (
            "Device verdict is based on the real physical LED measurement. "
            "Software simulations are not hardware measurements. "
            "Blocked and errored tests are not counted as passes."
        ),
    }

    with sqlite3.connect(DATABASE) as db:
        db.execute(
            """
            INSERT INTO test_suites
            (suite_id, timestamp, component, device_status,
             suite_status, report_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                suite_id,
                timestamp,
                label,
                device_status,
                suite_status,
                json.dumps(report, allow_nan=False),
            ),
        )
        db.commit()

    return report


@app.get("/api/ai/suite-history")
def get_suite_history(limit: int = 20):
    limit = max(1, min(limit, 100))

    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT report_json
            FROM test_suites
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [json.loads(row["report_json"]) for row in rows]

@app.get("/api/history")
def get_history(limit: int = 20):
    limit = max(1, min(limit, 100))

    with sqlite3.connect(DATABASE) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT run_id, timestamp, component, overall_status,
                   measurements_json, results_json
            FROM test_runs
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [
        {
            "run_id": row["run_id"],
            "timestamp": row["timestamp"],
            "component": row["component"],
            "overall_status": row["overall_status"],
            "measurements": json.loads(row["measurements_json"]),
            "results": json.loads(row["results_json"]),
        }
        for row in rows
    ]


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "embedded-test-backend"}
