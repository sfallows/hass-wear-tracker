"""Bus event helpers for anomaly notifications (DESIGN §10)."""
from __future__ import annotations

from homeassistant.core import HomeAssistant, callback

from .const import EVENT_CONNECTION_ANOMALY, EVENT_FLAP_ANOMALY


def _ratio(rate: float, baseline: float) -> float | None:
    return round(rate / baseline, 2) if baseline else None


@callback
def fire_flap_anomaly(
    hass: HomeAssistant, entity_id: str, flap_rate_1h: float, baseline: float
) -> None:
    hass.bus.async_fire(
        EVENT_FLAP_ANOMALY,
        {
            "entity_id": entity_id,
            "flap_rate_1h": round(flap_rate_1h, 3),
            "baseline_30d": round(baseline, 3),
            "ratio": _ratio(flap_rate_1h, baseline),
        },
    )


@callback
def fire_connection_anomaly(
    hass: HomeAssistant, entity_id: str, unavail_rate_1h: float, baseline: float
) -> None:
    hass.bus.async_fire(
        EVENT_CONNECTION_ANOMALY,
        {
            "entity_id": entity_id,
            "unavail_rate_1h": round(unavail_rate_1h, 3),
            "baseline_30d": round(baseline, 3),
            "ratio": _ratio(unavail_rate_1h, baseline),
        },
    )
