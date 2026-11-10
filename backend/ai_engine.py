
"""Rule-based AI test engineer for the Arduino LED fixture.

This module generates test plans and analyzes supplied test reports.
It does not control the Arduino and never invents measurements.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/ai", tags=["AI Test Engineer"])

SUPPORTED_COMPONENTS = {"led"}


class PlanRequest(BaseModel):
    component: str = "led"
    component_label: str = "LED A"


class AnalysisRequest(BaseModel):
    # Pass the actual report returned by /api/test/led.
    report: dict[str, Any]
    # Optional previous report for comparison.
    previous_report: dict[str, Any] | None = None


def make_case(
    test_id: str,
    category: str,
    purpose: str,
    preconditions: str,
    procedure: str,
    expected: str,
    execution: str,
) -> dict[str, Any]:
    return {
        "test_id": test_id,
        "category": category,
        "purpose": purpose,
        "preconditions": preconditions,
        "procedure": procedure,
        "expected": expected,
        "actual": None,
        "status": "PROPOSED",
        "execution": execution,
        "usefulness": "Not evaluated yet; the test has not been executed.",
    }


def generate_led_plan(label: str) -> list[dict[str, Any]]:
    """Create supported physical tests and safe software-only tests."""

    return [
        make_case(
            "LED-001",
            "normal",
            "Verify normal LED operation.",
            f"{label} is connected to the documented fixture.",
            "Run the existing physical LED test and capture both ADC readings.",
            "Measurements satisfy the configured red-LED acceptance limits.",
            "HARDWARE_SUPPORTED",
        ),
        make_case(
            "LED-002",
            "boundary",
            "Check the lower accepted current boundary.",
            "A saved measurement report is available.",
            "Feed a synthetic measurement at the configured lower current limit "
            "to the Python evaluator; do not drive the circuit.",
            "The documented boundary policy is applied consistently.",
            "SOFTWARE_SIMULATION_ONLY",
        ),
        make_case(
            "LED-003",
            "boundary",
            "Check the upper accepted current boundary.",
            "A saved measurement report is available.",
            "Feed a synthetic measurement at the configured upper current limit "
            "to the Python evaluator; do not drive the circuit.",
            "The documented boundary policy is applied consistently.",
            "SOFTWARE_SIMULATION_ONLY",
        ),
        make_case(
            "LED-004",
            "fault_injection",
            "Check open-circuit-like behavior.",
            "Use the approved open-circuit simulation in the fixture.",
            "Run the physical test with the simulated open circuit.",
            "The current is near zero and the evaluator reports the appropriate "
            "verdict without claiming a uniquely proven physical cause.",
            "HARDWARE_SUPPORTED_WITH_APPROVED_FIXTURE",
        ),
        make_case(
            "LED-005",
            "invalid_input",
            "Reject an ADC value outside the valid 10-bit range.",
            "The Python measurement validator is available.",
            "Supply a synthetic ADC value of 1100 to the validator.",
            "The validator rejects the report; no hardware command is sent.",
            "SOFTWARE_SIMULATION_ONLY",
        ),
        make_case(
            "LED-006",
            "invalid_input",
            "Reject missing or non-finite measurements.",
            "The Python measurement validator is available.",
            "Supply a report with a missing field, NaN or infinity.",
            "The validator rejects the invalid report; it is not classified as "
            "a component failure.",
            "SOFTWARE_SIMULATION_ONLY",
        ),
        make_case(
            "LED-007",
            "unexpected",
            "Check current-calculation consistency.",
            "A physical test report contains both ADC-derived voltages and current.",
            "Recalculate series current from the measured voltage drop and "
            "330-ohm resistor, then compare it with the reported series current.",
            "The difference is within a documented tolerance accounting for "
            "rounding and measurement resolution.",
            "SOFTWARE_ANALYSIS_ONLY",
        ),
        make_case(
            "LED-008",
            "regression",
            "Verify that a firmware update preserves the serial protocol.",
            "The Arduino is connected and the firmware is uploaded.",
            "Send ping, run a LED test and verify each response is valid JSON "
            "with the expected message type.",
            "Valid responses are returned; unsupported commands are rejected.",
            "HARDWARE_SUPPORTED",
        ),
    ]


@router.get("/status")
def ai_status() -> dict[str, Any]:
    return {
        "ok": True,
        "engine": "rule_based",
        "external_ai_required": False,
        "supported_components": sorted(SUPPORTED_COMPONENTS),
        "message": (
            "Rule-based test generation is available. "
            "Measurements and verdicts remain under deterministic control."
        ),
    }


@router.post("/plan")
def create_plan(request: PlanRequest) -> dict[str, Any]:
    component = request.component.strip().lower()
    label = request.component_label.strip()

    if component not in SUPPORTED_COMPONENTS:
        return {
            "ok": False,
            "error": "unsupported_component",
            "message": "Only the LED fixture is implemented in this version.",
        }

    if not label or len(label) > 40:
        return {
            "ok": False,
            "error": "invalid_component_label",
            "message": "The component label must contain 1 to 40 characters.",
        }

    cases = generate_led_plan(label)

    return {
        "ok": True,
        "component": component,
        "component_label": label,
        "generator": "rule_based",
        "test_count": len(cases),
        "cases": cases,
        "execution_policy": (
            "PROPOSED tests are not automatically executed. "
            "Only implemented, explicitly supported test procedures may run."
        ),
    }


def finite_number(value: Any) -> float | None:
    """Return a finite numeric value, excluding bool and invalid numbers."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    number = float(value)
    return number if math.isfinite(number) else None


def extract_measurements(report: dict[str, Any]) -> dict[str, Any]:
    measurements = report.get("measurements")

    if isinstance(measurements, dict):
        return measurements

    # Also support a raw Arduino measurement object.
    return report


@router.post("/analyze")
def analyze_report(request: AnalysisRequest) -> dict[str, Any]:
    report = request.report
    measurements = extract_measurements(report)

    required = [
        "voltage_a0_v",
        "voltage_a1_v",
        "series_current_ma",
        "led_current_ma",
    ]

    missing = [
        key for key in required
        if finite_number(measurements.get(key)) is None
    ]

    if missing:
        return {
            "ok": True,
            "analysis_status": "INCONCLUSIVE",
            "overall_status": report.get("overall_status"),
            "findings": [
                {
                    "severity": "warning",
                    "message": "The report is missing valid finite numeric "
                               "measurements.",
                    "evidence": {"missing_or_invalid_fields": missing},
                    "likely_causes": [
                        "Incomplete serial response",
                        "Invalid or incomplete stored report",
                    ],
                    "confidence": "limited",
                }
            ],
            "regression_tests": [
                "Test rejection of missing measurements.",
                "Test rejection of NaN and infinity.",
                "Verify serial timeout and malformed-response handling.",
            ],
            "measurement_source": "provided_report",
            "verdict_note": (
                "The analyzer does not change the backend verdict. "
                "Missing data is not proof of a faulty component."
            ),
        }

    a0 = float(measurements["voltage_a0_v"])
    a1 = float(measurements["voltage_a1_v"])
    series_current = float(measurements["series_current_ma"])
    led_current = float(measurements["led_current_ma"])

    findings: list[dict[str, Any]] = []
    regression_tests: list[str] = []

    # Unexpected test: independently recalculate current from measured
    # voltages and the documented nominal 330-ohm series resistor.
    calculated_ma = (a0 - a1) / 330.0 * 1000.0

    # The Arduino rounds voltage and current in its JSON response.
    # This is a provisional tolerance, not a calibrated instrument spec.
    current_difference = abs(calculated_ma - series_current)
    tolerance_ma = 0.15

    if current_difference > tolerance_ma:
        findings.append({
            "severity": "warning",
            "message": "Reported series current differs from the value "
                       "recalculated from the reported voltages.",
            "evidence": {
                "recalculated_series_current_ma": round(calculated_ma, 4),
                "reported_series_current_ma": series_current,
                "absolute_difference_ma": round(current_difference, 4),
                "provisional_tolerance_ma": tolerance_ma,
            },
            "likely_causes": [
                "Different rounding or calculation paths",
                "Stale or inconsistent measurements",
                "Incorrect resistor value in the calculation",
            ],
            "confidence": "medium",
        })
        regression_tests.append(
            "Recalculate series current from recorded voltages and verify "
            "the result within a calibrated tolerance."
        )
    else:
        findings.append({
            "severity": "info",
            "message": "The reported series current is consistent with "
                       "the voltage-drop calculation within the provisional "
                       "tolerance.",
            "evidence": {
                "recalculated_series_current_ma": round(calculated_ma, 4),
                "reported_series_current_ma": series_current,
                "absolute_difference_ma": round(current_difference, 4),
                "provisional_tolerance_ma": tolerance_ma,
            },
            "likely_causes": [],
            "confidence": "medium",
        })

    if led_current < 0 or series_current < 0:
        findings.append({
            "severity": "warning",
            "message": "A negative current was recorded.",
            "evidence": {
                "series_current_ma": series_current,
                "led_current_ma": led_current,
            },
            "likely_causes": [
                "Measurement noise",
                "Unexpected circuit wiring",
                "Current calculation or data-integrity issue",
            ],
            "confidence": "high",
        })
        regression_tests.append(
            "Verify that the evaluator rejects physically invalid "
            "measurement combinations."
        )

    previous = request.previous_report
    if previous:
        old = extract_measurements(previous)
        old_a1 = finite_number(old.get("voltage_a1_v"))
        old_current = finite_number(old.get("led_current_ma"))

        if old_a1 is not None and old_current is not None:
            findings.append({
                "severity": "info",
                "message": "Previous-run comparison is available.",
                "evidence": {
                    "previous_voltage_a1_v": old_a1,
                    "current_voltage_a1_v": a1,
                    "voltage_change_v": round(a1 - old_a1, 4),
                    "previous_led_current_ma": old_current,
                    "current_led_current_ma": led_current,
                    "current_change_ma": round(led_current - old_current, 4),
                },
                "likely_causes": [],
                "confidence": "high",
            })
            regression_tests.append(
                "Compare repeated measurements of the same reference LED "
                "to establish normal run-to-run variation."
            )

    return {
        "ok": True,
        "analysis_status": "COMPLETE",
        "overall_status": report.get("overall_status"),
        "component": report.get("component", "Unknown component"),
        "findings": findings,
        "regression_tests": regression_tests or [
            "Repeat the test with the same reference LED and compare "
            "the measurements."
        ],
        "measurement_source": "provided_report",
        "verdict_note": (
            "This analysis describes evidence and possible causes. "
            "It does not invent measurements or modify the official "
            "PASS/FAIL verdict."
        ),
    }
