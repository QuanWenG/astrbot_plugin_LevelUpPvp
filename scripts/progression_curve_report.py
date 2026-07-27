"""Print old/new progression milestones for balance review."""

import math
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.progression_rules import (
    EXP_SCALE,
    attribute_exp_required,
    legacy_attribute_exp_required,
    legacy_skill_exp_required,
    legacy_spell_exp_required,
    skill_exp_required,
    spell_exp_required,
)


MILESTONES = (1, 10, 20, 50, 100)
MAX_RAW_EXP_PER_BATTLE = 20


def _row(label, value, old_required, new_required):
    old_internal = old_required * EXP_SCALE
    battles = math.ceil(new_required / (MAX_RAW_EXP_PER_BATTLE * EXP_SCALE))
    return (
        f"{label:<8} {value:>3}  old={old_internal:>7}  "
        f"new={new_required:>7}  ratio={new_required / old_internal:>5.2f}  "
        f"battles@100%={battles:>3}"
    )


def main() -> None:
    for value in MILESTONES:
        print(
            _row(
                "attribute",
                value,
                legacy_attribute_exp_required(value),
                attribute_exp_required(value),
            )
        )
    for value in MILESTONES:
        print(
            _row(
                "skill",
                value,
                legacy_skill_exp_required(value),
                skill_exp_required(value),
            )
        )
    for value in MILESTONES:
        print(
            _row(
                "spell",
                value,
                legacy_spell_exp_required(value),
                spell_exp_required(value),
            )
        )


if __name__ == "__main__":
    main()
