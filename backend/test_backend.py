
import pytest

from main import evaluate_led, validate_measurements


def normal_led():
    return {
        "adc_a0": 998.0,
        "adc_a1": 368.0,
        "voltage_a0_v": 4.8876,
        "voltage_a1_v": 1.7986,
        "series_current_ma": 9.3606,
        "led_current_ma": 9.3426,
    }


def open_led():
    return {
        "adc_a0": 1018.0,
        "adc_a1": 1005.0,
        "voltage_a0_v": 4.9756,
        "voltage_a1_v": 4.9120,
        "series_current_ma": 0.1927,
        "led_current_ma": 0.1436,
    }


def short_led():
    return {
        "adc_a0": 1000.0,
        "adc_a1": 20.0,
        "voltage_a0_v": 4.8876,
        "voltage_a1_v": 0.0978,
        "series_current_ma": 14.5145,
        "led_current_ma": 14.5135,
    }


def test_normal_red_led_passes():
    result = evaluate_led(normal_led())
    assert result["overall_status"] == "PASS"


def test_open_led_fails():
    result = evaluate_led(open_led())
    assert result["overall_status"] == "FAIL"
    assert "Open-circuit-like" in result["diagnosis"]


def test_short_led_fails():
    result = evaluate_led(short_led())
    assert result["overall_status"] == "FAIL"
    assert "Short-circuit-like" in result["diagnosis"]


def test_adc_out_of_range_is_rejected():
    data = normal_led()
    data["adc_a0"] = 1100

    with pytest.raises(ValueError):
        validate_measurements(data)


def test_nan_measurement_is_rejected():
    data = normal_led()
    data["voltage_a1_v"] = float("nan")

    with pytest.raises(ValueError):
        validate_measurements(data)


def test_missing_measurement_is_rejected():
    data = normal_led()
    del data["led_current_ma"]

    with pytest.raises(ValueError):
        validate_measurements(data)
