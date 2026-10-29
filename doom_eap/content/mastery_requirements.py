"""Scale qualified native mastery counters from their authored original targets."""

import re


MASTERY_REQUIREMENT_DIVISORS = {"Original": 1, "Half": 2, "Quarter": 4}


def mastery_entries_for_counts(entries, counts):
    return tuple({**entry, "signal": {**entry["signal"],
                  "rule_0_statCount": counts[entry["signal"]["unlockable"]]}} for entry in entries)


def mastery_entries_for_profile(entries, profile):
    try:
        divisor = MASTERY_REQUIREMENT_DIVISORS[profile]
    except KeyError as error:
        raise ValueError(f"Unsupported Mastery Requirements: {profile!r}") from error
    result = []
    for entry in entries:
        requirement = entry["requirement"]
        count = requirement["original_count"]
        if requirement["kind"] in {"repetitions", "damage"}:
            count = max(1, (count + divisor - 1) // divisor)
        result.append({**entry, "signal": {**entry["signal"], "rule_0_statCount": count}})
    return tuple(result)


def patch_mastery_decl(source, entry):
    signal = entry["signal"]
    original_count = entry["requirement"]["original_count"]
    rule = re.compile(
        r'(\brules\s*=\s*\{\s*item\[0\]\s*=\s*\{\s*stat\s*=\s*"'
        + re.escape(signal["rule_0_statname"])
        + r'";\s*statDuration\s*=\s*"DUR_CUSTOM";\s*count\s*=\s*)'
        + str(original_count) + r'(;)',
    )
    result, count = rule.subn(lambda match: f'{match[1]}{signal["rule_0_statCount"]}{match[2]}', source)
    if count != 1:
        raise ValueError(f"{entry['name']}: original mastery count is missing or ambiguous")
    return result
