"""Battery limits shared by campaign generation and room validation."""


def battery_surplus_limit(cost, percent):
    return (cost * percent + 99) // 100


def validate_battery_economy(economy, *, cost, percent, native_first_battery):
    surplus = economy.get("surplus") if isinstance(economy, dict) else None
    if (type(surplus) is not int or not 0 <= surplus <= battery_surplus_limit(cost, percent)
            or economy != {
                "cost": cost, "surplus": surplus, "total": cost + surplus,
                "surplus_percent": percent, "native_first_battery": native_first_battery,
            }):
        raise ValueError("Fortress Battery economy contradicts its native spend groups")
