"""Stateless, named random streams for reproducible combat.

Sequential PRNGs make seemingly harmless changes dangerous: inserting one flavour
roll near the start shifts every hit, proc, and reward after it.  ``KeyedEntropy``
instead hashes the complete identity of a draw.  Adding a new draw under another
stream/subindex cannot perturb any existing outcome.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import TypeVar


T = TypeVar("T")
_PERSONALIZATION = b"pvp-keyed-rng-v1"
_UNIT_DENOMINATOR = 1 << 53


class KeyedEntropy:
    """Deterministic entropy addressed by battle coordinates, not call order.

    Every method is pure for a given key.  Callers use ``subindex`` for multiple
    draws belonging to the same action (for example hit=0, crit=1, damage=2), and
    distinct ``stream`` names for unrelated systems such as combat and rewards.
    """

    def __init__(self, ruleset_id: str, seed: int | str) -> None:
        if not isinstance(ruleset_id, str):
            raise TypeError("ruleset_id must be a string")
        if not ruleset_id or ruleset_id.strip() != ruleset_id:
            raise ValueError("ruleset_id must be a non-empty exact id")
        if ruleset_id.casefold() == "latest":
            raise ValueError("'latest' cannot identify reproducible entropy")
        if not isinstance(seed, (int, str)):
            raise TypeError("seed must be an int or string")
        self._ruleset_id = ruleset_id
        # Preserve the type so integer 7 and externally supplied string "7" do
        # not accidentally address the same battle.
        self._seed = f"{type(seed).__name__}:{seed}"

    @property
    def ruleset_id(self) -> str:
        return self._ruleset_id

    def _digest(
        self,
        *,
        stream: str,
        tick: int,
        actor: str | int | None,
        action_seq: int,
        subindex: int,
    ) -> bytes:
        if not isinstance(stream, str):
            raise TypeError("stream must be a string")
        if not stream or stream.strip() != stream:
            raise ValueError("stream must be a non-empty exact name")
        for label, coordinate in (
            ("tick", tick),
            ("action_seq", action_seq),
            ("subindex", subindex),
        ):
            if not isinstance(coordinate, int):
                raise TypeError(f"{label} must be an integer")
            if coordinate < 0:
                raise ValueError(f"{label} must not be negative")

        actor_value = f"{type(actor).__name__}:{actor}"
        parts = (
            self._ruleset_id,
            self._seed,
            stream,
            str(tick),
            actor_value,
            str(action_seq),
            str(subindex),
        )
        # Length prefixes make the encoding unambiguous even if a stream or actor
        # contains the human-readable separator used in design documentation.
        payload = bytearray()
        for part in parts:
            encoded = part.encode("utf-8")
            payload.extend(len(encoded).to_bytes(4, "big"))
            payload.extend(encoded)
        return hashlib.blake2b(
            payload,
            digest_size=16,
            person=_PERSONALIZATION,
        ).digest()

    def random(
        self,
        *,
        stream: str,
        tick: int = 0,
        actor: str | int | None = None,
        action_seq: int = 0,
        subindex: int = 0,
    ) -> float:
        """Return a stable float in ``[0.0, 1.0)`` for the complete key."""

        digest = self._digest(
            stream=stream,
            tick=tick,
            actor=actor,
            action_seq=action_seq,
            subindex=subindex,
        )
        # Use 53 high bits, matching the precision available to a Python float.
        value = int.from_bytes(digest, "big") >> (128 - 53)
        return value / _UNIT_DENOMINATOR

    def uniform(
        self,
        a: float,
        b: float,
        *,
        stream: str,
        tick: int = 0,
        actor: str | int | None = None,
        action_seq: int = 0,
        subindex: int = 0,
    ) -> float:
        unit = self.random(
            stream=stream,
            tick=tick,
            actor=actor,
            action_seq=action_seq,
            subindex=subindex,
        )
        return float(a) + (float(b) - float(a)) * unit

    def randint(
        self,
        a: int,
        b: int,
        *,
        stream: str,
        tick: int = 0,
        actor: str | int | None = None,
        action_seq: int = 0,
        subindex: int = 0,
    ) -> int:
        """Return a stable integer in the inclusive range ``[a, b]``."""

        if not isinstance(a, int) or not isinstance(b, int):
            raise TypeError("randint bounds must be integers")
        if a > b:
            raise ValueError("randint lower bound must not exceed upper bound")
        span = b - a + 1
        unit = self.random(
            stream=stream,
            tick=tick,
            actor=actor,
            action_seq=action_seq,
            subindex=subindex,
        )
        return a + min(span - 1, math.floor(unit * span))

    def choice(
        self,
        population: Sequence[T],
        *,
        stream: str,
        tick: int = 0,
        actor: str | int | None = None,
        action_seq: int = 0,
        subindex: int = 0,
    ) -> T:
        if not population:
            raise IndexError("cannot choose from an empty population")
        index = self.randint(
            0,
            len(population) - 1,
            stream=stream,
            tick=tick,
            actor=actor,
            action_seq=action_seq,
            subindex=subindex,
        )
        return population[index]

    def weighted_choice(
        self,
        population: Sequence[T],
        weights: Sequence[float],
        *,
        stream: str,
        tick: int = 0,
        actor: str | int | None = None,
        action_seq: int = 0,
        subindex: int = 0,
    ) -> T:
        """Choose from positive finite weights using a stable keyed draw."""

        if not population:
            raise IndexError("cannot choose from an empty population")
        if len(population) != len(weights):
            raise ValueError("population and weights must have equal length")
        numeric_weights = tuple(float(weight) for weight in weights)
        if any(not math.isfinite(weight) or weight < 0 for weight in numeric_weights):
            raise ValueError("weights must be finite and non-negative")
        total = math.fsum(numeric_weights)
        if total <= 0:
            raise ValueError("at least one weight must be positive")

        needle = self.random(
            stream=stream,
            tick=tick,
            actor=actor,
            action_seq=action_seq,
            subindex=subindex,
        ) * total
        cumulative = 0.0
        last_positive = population[0]
        for item, weight in zip(population, numeric_weights):
            if weight > 0:
                last_positive = item
            cumulative += weight
            if needle < cumulative:
                return item
        # Only reachable through floating point accumulation at the upper edge.
        return last_positive


class KeyedRandomStream:
    """Small ``random.Random``-shaped adapter for one named combat subsystem.

    Legacy runtime code expects a stateful object with ``random``/``uniform``/
    ``randrange``.  This adapter confines that state to a named stream and one
    tick/action coordinate.  Adding a flavour roll to AI selection can therefore
    never shift hit, critical, status or loot results.
    """

    def __init__(
        self,
        entropy: KeyedEntropy,
        *,
        stream: str,
        tick: int = 0,
        actor: str | int | None = None,
        action_seq: int = 0,
    ) -> None:
        self._entropy = entropy
        self._stream = stream
        self._tick = tick
        self._actor = actor
        self._action_seq = action_seq
        self._subindex = 0

    def _take(self) -> int:
        subindex = self._subindex
        self._subindex += 1
        return subindex

    def random(self) -> float:
        return self._entropy.random(
            stream=self._stream,
            tick=self._tick,
            actor=self._actor,
            action_seq=self._action_seq,
            subindex=self._take(),
        )

    def uniform(self, a: float, b: float) -> float:
        return float(a) + (float(b) - float(a)) * self.random()

    def randint(self, a: int, b: int) -> int:
        if a > b:
            raise ValueError("empty range for randint")
        return a + min(b - a, math.floor(self.random() * (b - a + 1)))

    def randrange(
        self,
        start: int,
        stop: int | None = None,
        step: int = 1,
    ) -> int:
        if step == 0:
            raise ValueError("zero step for randrange")
        if stop is None:
            start, stop = 0, start
        population = range(start, stop, step)
        if len(population) <= 0:
            raise ValueError("empty range for randrange")
        return population[min(
            len(population) - 1,
            math.floor(self.random() * len(population)),
        )]

    def choice(self, population: Sequence[T]) -> T:
        if not population:
            raise IndexError("cannot choose from an empty population")
        return population[self.randrange(len(population))]
