"""Deterministic BRUTE/COMBO pattern generation for Kick jobs.

This module only decides target batches/waves. It never opens sockets, sends
commands, mutates target state, or handles replacement. Keeping those concerns
separate makes the job runner easier to test and prevents transport changes
from accidentally changing BRUTE/COMBO semantics.
"""

class PatternEngine:
    COMBO_BURSTS = {
        "combo1": (2, 3),
        "combo2": (3, 4),
    }

    def __init__(self, targets, burst=1, combo="off"):
        self.targets = [str(x).strip() for x in (targets or []) if str(x).strip()]
        self.combo = str(combo or "off").strip().lower()
        self.burst = max(1, min(10, int(burst or 1)))
        if self.combo not in {"off", "combo1", "combo2"}:
            self.combo = "off"

    @staticmethod
    def _chunks(values, size):
        size = max(1, int(size))
        return [values[i:i + size] for i in range(0, len(values), size)]

    def waves(self):
        """Return waves exactly matching the v4 BRUTE/COMBO scheduling semantics."""
        if self.combo in self.COMBO_BURSTS:
            b1, b2 = self.COMBO_BURSTS[self.combo]
            batches1 = self._chunks(self.targets, b1)
            batches2 = self._chunks(self.targets, b2)
            waves = []
            for i in range(max(len(batches1), len(batches2))):
                wave = []
                if i < len(batches1):
                    wave.extend(batches1[i])
                if i < len(batches2):
                    wave.extend(batches2[i])
                if wave:
                    waves.append(wave)
            return waves
        return self._chunks(self.targets, self.burst)

    def wave_count(self):
        return len(self.waves())

    def jobs_per_socket(self):
        return sum(len(wave) for wave in self.waves())

    def describe(self):
        if self.combo in self.COMBO_BURSTS:
            a, b = self.COMBO_BURSTS[self.combo]
            return f"{self.combo}: BRUTE{a} + BRUTE{b}"
        return f"BRUTE{self.burst}"
