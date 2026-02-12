import time
import re
import socket
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

import azcam
import azcam.exceptions
from azcam.tools.instrument import Instrument


# =========================
# INDI socket client
# =========================

@dataclass
class IndiConfig:
    host: str = "10.0.1.108"
    port: int = 7600
    timeout: float = 1.0
    recv_chunk: int = 4096


class VattIndiGuidebox:
    """
    Minimal INDI-over-TCP helper for the VATT guidebox INDI driver.
    Uses XML over a raw TCP socket.
    """

    CONTROL_DEVICE = "INDI-VATT-GUIDEBOX"   # mydev in gb_indi.c
    QUERY_DEVICE = "FILTERS"                # qrydev in gb_indi.c

    LOWER_NAMES_PROP = "LOWER_FNAMES"
    UPPER_NAMES_PROP = "UPPER_FNAMES"

    LOWER_GOTO_PROP = "FWHEEL_LOWER"
    UPPER_GOTO_PROP = "FWHEEL_UPPER"

    def __init__(self, cfg: Optional[IndiConfig] = None):
        self.cfg = cfg or IndiConfig()

    def _connect(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.cfg.timeout)
        s.connect((socket.gethostbyname(self.cfg.host), int(self.cfg.port)))
        return s

    def _send_recv(self, payload: str, max_wait: float, until=None) -> str:
        data = ""
        t0 = time.time()
        with self._connect() as s:
            s.sendall((payload.strip() + "\n").encode("utf-8"))
            while True:
                if (time.time() - t0) > max_wait:
                    break
                try:
                    chunk = s.recv(self.cfg.recv_chunk).decode("utf-8", errors="ignore")
                except socket.timeout:
                    break
                if not chunk:
                    break
                data += chunk

                if until:
                    if until.search(data):
                        break
                else:
                    if data.rstrip().endswith(">"):
                        break

        return data

    def getfilters(self, retries: int = 3, delay: float = 0.2) -> Dict[str, str]:
        """
        Read current in-beam filter labels using QUERY_DEVICE IDMessage response.
        Expects message like: upper:Clear lower:U
        """
        regex = re.compile(r'message="upper:(.+?)\s+lower:(.+?)"')
        last = None

        for _ in range(max(1, retries)):
            xml = self._send_recv(
                f"<getProperties version='1.7' device='{self.QUERY_DEVICE}' />",
                max_wait=max(1.5, self.cfg.timeout),
                until=regex,
            )
            last = xml
            m = regex.search(xml)
            if m:
                return {"upper": m.group(1).strip(), "lower": m.group(2).strip()}
            time.sleep(delay)

        raise RuntimeError(f"Could not parse FILTERS response: {last!r}")

    def get_wheel_names(self, wheel: str) -> Dict[int, str]:
        """
        Returns slot->label for wheel ('upper' or 'lower') by parsing the
        text vector property LOWER_FNAMES / UPPER_FNAMES.
        """
        wheel = wheel.lower().strip()
        if wheel not in ("upper", "lower"):
            raise ValueError("wheel must be 'upper' or 'lower'")

        prop = self.UPPER_NAMES_PROP if wheel == "upper" else self.LOWER_NAMES_PROP

        until = re.compile(r'<(defText|oneText)\s+name="F4"')
        xml = self._send_recv(
            f"<getProperties version='1.7' device='{self.CONTROL_DEVICE}' name='{prop}' />",
            max_wait=max(1.5, self.cfg.timeout),
            until=until
        )

        # defText and oneText variants
        slotmap: Dict[int, str] = {}

        for name, val in re.findall(r'<defText\s+name="(F\d)".*?>(.*?)</defText>', xml, re.S):
            idx = int(name[1:])
            slotmap[idx] = re.sub(r"\s+", " ", val).strip()

        for name, val in re.findall(r'<oneText\s+name="(F\d)".*?>(.*?)</oneText>', xml, re.S):
            idx = int(name[1:])
            slotmap[idx] = re.sub(r"\s+", " ", val).strip()

        # INDI event model might not provide us all 5 slots immediately. Just warn if too little appear (less than 3) 
        # as something might be wrong.
        if len(slotmap) < 3:
            azcam.log(f"WARNING: parsed only {len(slotmap)} names for {wheel} wheel from {prop}")

        return slotmap

    def set_wheel_slot(self, wheel: str, slot: int) -> None:
        """
        Command wheel goto by slot number via newNumberVector.
        """
        wheel = wheel.lower().strip()
        if wheel not in ("upper", "lower"):
            raise ValueError("wheel must be 'upper' or 'lower'")

        slot = int(slot)
        if not (0 <= slot <= 4):
            raise ValueError("slot must be 0..4")

        prop = self.UPPER_GOTO_PROP if wheel == "upper" else self.LOWER_GOTO_PROP
        xml = (
            f"<newNumberVector device='{self.CONTROL_DEVICE}' name='{prop}'>"
            f"<oneNumber name='{prop}'>{slot}</oneNumber>"
            f"</newNumberVector>"
        )
        # fire-and-forget; confirmation handled by caller
        self._send_recv(xml, max_wait=max(0.5, self.cfg.timeout))


# =========================
# AzCam Instrument tool
# =========================

class VattInstrumentIndi(Instrument):
    """
    Implements AzCam Instrument filter API using VATT guidebox INDI driver.

    IMPORTANT:
      - Observers MUST specify wheel explicitly: 'upper:<x>' or 'lower:<x>'.
    """

    def __init__(self, tool_id="instrument", description="VATT instrument (INDI guidebox filters)"):
        super().__init__(tool_id, description)
        self.indi = VattIndiGuidebox(IndiConfig())

        # tiny cache to reduce INDI traffic
        self._cache_sec = 1.0
        self._names_cache = {"upper": None, "lower": None}
        self._names_cache_t = 0.0

    # -------- internal helpers --------

    def _names(self, wheel: str) -> Dict[int, str]:
        now = time.time()
        if (now - self._names_cache_t) > self._cache_sec or self._names_cache[wheel] is None:
            self._names_cache["upper"] = self.indi.get_wheel_names("upper")
            self._names_cache["lower"] = self.indi.get_wheel_names("lower")
            self._names_cache_t = now
        return dict(self._names_cache[wheel])

    def _parse_wheel_value(self, raw: str) -> Tuple[str, str]:
        """
        Require explicit wheel prefix: 'upper:...' or 'lower:...'
        Returns (wheel, value).
        """
        raw = str(raw).strip()
        if ":" not in raw:
            raise azcam.exceptions.AzcamError(
                "Filter wheel must be specified explicitly. "
                "Use 'upper:<name|slot>' or 'lower:<name|slot>'."
            )
        prefix, rest = raw.split(":", 1)
        wheel = prefix.strip().lower()
        value = rest.strip()

        if wheel not in ("upper", "lower"):
            raise azcam.exceptions.AzcamError(
                f"Unknown wheel prefix '{prefix}'. Use 'upper:' or 'lower:'."
            )
        if value == "":
            raise azcam.exceptions.AzcamError("Missing filter value after wheel prefix.")
        return wheel, value

    def _label_to_slot(self, wheel: str, label: str) -> Optional[int]:
        label = (label or "").strip()
        names = self._names(wheel)
        for slot, lab in names.items():
            if (lab or "").strip() == label:
                return slot
        return None

    def _resolve_to_slot(self, wheel: str, value: str) -> int:
        """
        Resolve value to slot. Value can be:
          - digit 0..4
          - exact label match (case-sensitive, because observers define them)
        """
        if value.isdigit():
            slot = int(value)
            if 0 <= slot <= 4:
                return slot
            raise azcam.exceptions.AzcamError(f"Slot must be 0..4, got '{value}'.")

        slot = self._label_to_slot(wheel, value)
        if slot is None:
            labels = [v for _, v in sorted(self._names(wheel).items())]
            raise azcam.exceptions.AzcamError(
                f"Filter '{value}' not found on {wheel} wheel. Current {wheel} labels: {labels}"
            )
        return slot

    def _wait_confirm(self, wheel: str, expected_label: str, timeout: float = 90.0, poll: float = 1) -> None:
        t0 = time.time()
        while True:
            cur = self.indi.getfilters(retries=1)
            cur_label = (cur.get(wheel) or "").strip()
            if cur_label == expected_label:
                return
            if (time.time() - t0) > timeout:
                raise azcam.exceptions.AzcamError(
                    f"Timeout waiting for {wheel} wheel to reach '{expected_label}'. "
                    f"Currently '{cur_label}'."
                )
            time.sleep(poll)

    # -------- required AzCam Instrument API --------

    def get_filters(self, filter_id=0) -> List[str]:
        """
        Return current labels for a wheel.
          filter_id=1 -> upper labels
          filter_id=2 -> lower labels
          filter_id=0 -> combined (prefixed) labels: ['upper:X', 'lower:Y', ...]
        """
        if filter_id == 1:
            return [v for _, v in sorted(self._names("upper").items())]
        if filter_id == 2:
            return [v for _, v in sorted(self._names("lower").items())]

        out = []
        for wheel in ("upper", "lower"):
            for _, v in sorted(self._names(wheel).items()):
                out.append(f"{wheel}:{v}")
        return out

    def get_filter(self, filter_id=0) -> str:
        """
        Return current in-beam filters.
        filter_id=0 returns both: 'upper:<u> lower:<l>'
        filter_id=1 returns upper only
        filter_id=2 returns lower only
        """
        cur = self.indi.getfilters(retries=3)
        u = (cur.get("upper") or "").strip()
        l = (cur.get("lower") or "").strip()
        if filter_id == 1:
            return u
        if filter_id == 2:
            return l
        return f"upper:{u} lower:{l}"

    def set_filter(self, filter_name, filter_id=0):
        """
        Set a wheel filter. REQUIRE explicit wheel prefix.
        Examples:
          set_filter('upper:U') # filter name
          set_filter('lower:3') # filter position
        """
        wheel, value = self._parse_wheel_value(filter_name)

        # Resolve to slot
        slot = self._resolve_to_slot(wheel, value)

        # Determine expected label from live names
        names = self._names(wheel)
        expected_label = (names.get(slot) or "").strip()
        if expected_label == "":
            raise azcam.exceptions.AzcamError(
                f"Resolved slot {slot} on {wheel} wheel but label is empty/unknown."
            )

        azcam.log(f"Setting {wheel} wheel to slot {slot} ('{expected_label}')")

        # Command move
        self.indi.set_wheel_slot(wheel, slot)

        # Confirm via FILTERS telemetry
        self._wait_confirm(wheel, expected_label, timeout=90.0, poll=1)

        # Return both wheels state
        return self.get_filter(filter_id=0)
