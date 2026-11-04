"""Adapt shared Core wire results for scope and failure scenarios."""

from dataclasses import replace

from native_transport_support import evidence_result

from doom_eap.runtime.inventory_observation import (
    currency_grant_result,
    evidence_scope,
    progression_observation,
)


class EvidenceCore:
    def __init__(self, scope=None):
        self.scope = scope or evidence_scope(evidence_result("currency_result")["before_scope"])

    def grant(self, reference, *, amount=1, balance=0, gained=4):
        result = evidence_result("currency_result")
        result.update(amount=amount, expected_gained=gained, balance_before=balance, gained_before=gained,
                      balance_after=balance + amount, gained_after=gained + amount)
        return replace(currency_grant_result(result, reference), before_scope=self.scope, after_scope=self.scope)

    def progression(self, **fields):
        result = evidence_result("progression_result")
        result.update(suit_known=0, suit_owned=0, slayer_key_quantity=-1, meat_hook=0)
        return replace(progression_observation(result), before_scope=self.scope, after_scope=self.scope, **fields)
