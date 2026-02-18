import time
import re
import socket
import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List
import xml.etree.ElementTree as ET

import azcam
import azcam.exceptions
from azcam.tools.instrument import Instrument


# =========================
# INDI socket client config
# =========================

@dataclass
class IndiConfig:
    host: str = "10.0.1.108"
    port: int = 7600
    timeout: float = 1.0
    recv_chunk: int = 4096


# =========================
# Shared low-level INDI TCP helper
# =========================

class IndiTcpClient:
    """
    Minimal INDI-over-TCP helper using XML over a raw TCP socket.
    One request per connection.
    """

    def __init__(self, cfg: Optional[IndiConfig] = None):
        self.cfg = cfg or IndiConfig()

    def _connect(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.cfg.timeout)
        s.connect((socket.gethostbyname(self.cfg.host), int(self.cfg.port)))
        return s

    def send_recv(self, payload: str, max_wait: float, until: Optional[re.Pattern] = None) -> str:
        """
        Send payload and read until:
          - regex 'until' matches, OR
          - timeout, OR
          - socket closes / no more data / timeout on recv
        """
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

                if until and until.search(data):
                    break
        return data


# =========================
# Guidebox (filters)
# =========================

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
        self._tcp = IndiTcpClient(self.cfg)

    def _send_recv(self, payload: str, max_wait: float, until=None) -> str:
        return self._tcp.send_recv(payload, max_wait=max_wait, until=until)

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
# Secondary (focus)
# =========================

class VattIndiSecondary:
    """
    Minimal polling-style INDI client for VATT Secondary focus (PosZ).

    Protocol:
      - read a short buffer from INDI
      - parse XML elements by wrapping in a fake root
      - if buffer is truncated, use XMLPullParser to salvage complete stanzas
      - pick the latest setNumberVector (preferred) or defNumberVector (fallback)
      - extract Z value + vector state
    """

    DEVICE = "VATT Secondary"
    FOCUS_PROP = "PosZ"
    FOCUS_ELEM = "Z"

    def __init__(self, cfg: Optional[IndiConfig] = None):
        self.cfg = cfg or IndiConfig()
        self._tcp = IndiTcpClient(self.cfg)

    def _read_posz_buffer(self, max_wait: Optional[float] = None) -> str:
        if max_wait is None:
            max_wait = max(1.6, getattr(self.cfg, "timeout", 1.0))

        return self._tcp.send_recv(
            f"<getProperties version='1.7' device='{self.DEVICE}' name='{self.FOCUS_PROP}' />",
            max_wait=max_wait,
            until=None,
        )

    def _iter_vector_elements_wrapped(self, buf: str):
        wrapped = f"<root>{buf}</root>"
        root = ET.fromstring(wrapped)
        for child in root:
            if child.tag in ("setNumberVector", "defNumberVector"):
                yield child

    def _iter_vector_elements_pull(self, buf: str):
        """
        Streaming parse fallback for truncated buffers.
        """
        parser = ET.XMLPullParser(events=("end",))
        parser.feed("<root>")
        parser.feed(buf)

        for _event, elem in parser.read_events():
            if elem.tag in ("setNumberVector", "defNumberVector"):
                yield elem

        try:
            parser.feed("</root>")
            for _event, elem in parser.read_events():
                if elem.tag in ("setNumberVector", "defNumberVector"):
                    yield elem
            parser.close()
        except ET.ParseError:
            # buffer was truncated; we already yielded any completed elements
            return

    def _select_latest_posz_vector(self, buf: str) -> ET.Element:
        if not buf:
            raise RuntimeError("Empty INDI response buffer.")

        candidates_set = []
        candidates_def = []

        try:
            it = self._iter_vector_elements_wrapped(buf)
        except ET.ParseError:
            it = self._iter_vector_elements_pull(buf)

        for elem in it:
            dev = (elem.attrib.get("device") or "").strip()
            name = (elem.attrib.get("name") or "").strip()
            if dev != self.DEVICE or name != self.FOCUS_PROP:
                continue

            if elem.tag == "setNumberVector":
                candidates_set.append(elem)
            else:
                candidates_def.append(elem)

        if candidates_set:
            return candidates_set[-1]
        if candidates_def:
            return candidates_def[-1]

        raise RuntimeError(f"No {self.DEVICE}.{self.FOCUS_PROP} NumberVector found in buffer.")

    def _parse_posz_vector(self, vec: ET.Element) -> Tuple[float, str]:
        state = (vec.attrib.get("state") or "").strip()

        z_text = None
        for child in vec.findall(".//oneNumber") + vec.findall(".//defNumber"):
            if (child.attrib.get("name") or "") == self.FOCUS_ELEM:
                if child.text is not None:
                    z_text = child.text.strip()
                break

        if z_text is None:
            raise RuntimeError("PosZ vector missing Z value.")

        try:
            return float(z_text), state
        except ValueError:
            raise RuntimeError(f"PosZ Z value not numeric: {z_text!r}")

    def get_focus_z_and_state(self, retries: int = 3, delay: float = 0.15) -> Tuple[float, str]:
        last_buf = None
        for _ in range(max(1, retries)):
            try:
                last_buf = self._read_posz_buffer()
                vec = self._select_latest_posz_vector(last_buf)
                return self._parse_posz_vector(vec)
            except Exception:
                time.sleep(delay)

        raise RuntimeError(
            f"Could not read/parse {self.DEVICE}.{self.FOCUS_PROP}. "
            f"last_buf_tail={(last_buf or '')[-500:]!r}"
        )

    def get_focus_z(self, retries: int = 3, delay: float = 0.15) -> float:
        z, _state = self.get_focus_z_and_state(retries=retries, delay=delay)
        return z

    def set_focus_z(self, value: float) -> None:
        v = float(value)
        xml = (
            f"<newNumberVector device='{self.DEVICE}' name='{self.FOCUS_PROP}'>"
            f"<oneNumber name='{self.FOCUS_ELEM}'>{v}</oneNumber>"
            f"</newNumberVector>"
        )
        self._tcp.send_recv(xml, max_wait=max(0.5, getattr(self.cfg, "timeout", 1.0)), until=None)

    def wait_focus(
        self,
        expected: float,
        tol: float = 0.5,
        settle_eps: float = 0.05,
        timeout: float = 30.0,
        poll: float = 0.8,
        min_wait: float = 1.2,
    ) -> float:
        t0 = time.time()
        stable_hits = 0
        prev_val: Optional[float] = None

        while True:
            cur, state = self.get_focus_z_and_state(retries=2, delay=0.1)
            state_norm = (state or "").strip().lower()

            if state_norm == "alert":
                raise RuntimeError(f"Focus entered ALERT state (last={cur})")

            done_state = (state_norm == "ok")
            done_val = abs(cur - expected) <= tol
            done_settle = (prev_val is not None) and (abs(cur - prev_val) <= settle_eps)
            enough_time = (time.time() - t0) >= min_wait

            if enough_time and done_state and done_val and done_settle:
                stable_hits += 1
                if stable_hits >= 2:
                    return cur
            else:
                stable_hits = 0

            prev_val = cur

            if (time.time() - t0) > timeout:
                raise RuntimeError(f"Timeout waiting for focus {expected}. last={cur} state={state}")

            time.sleep(poll)


# =========================
# AzCam Instrument tool
# =========================

class VattInstrumentIndi(Instrument):
    """
    AzCam Instrument tool:
      - Filter control via VattIndiGuidebox
      - Focus control via VattIndiSecondary

    Observers can do focus in python directly:
      azcam.db.tools["instrument"].get_focus()
      azcam.db.tools["instrument"].set_focus(value, focus_type="absolute")
      azcam.db.tools["instrument"].set_focus(delta, focus_type="step")

    IMPORTANT:
      - Observers MUST specify wheel explicitly: 'upper:<x>' or 'lower:<x>'.
    """

    def __init__(self, tool_id="instrument", description="VATT instrument (INDI guidebox filters + secondary focus)"):
        super().__init__(tool_id, description)

        self.indi = VattIndiGuidebox(
            IndiConfig(host="10.0.1.108", port=7600, timeout=1.0)
        )

        # tiny cache to reduce INDI traffic
        self._cache_sec = 1.0
        self._names_cache = {"upper": None, "lower": None}
        self._names_cache_t = 0.0

        # lock for filter moves
        self._move_lock = threading.Lock()

        self.secondary = VattIndiSecondary(
            IndiConfig(host="10.0.1.108", port=7600, timeout=1.0)
        )

        self._focus_lock = threading.Lock()

        # keep a local cached last focus
        self._last_focus: Optional[float] = None

    # -------- internal helpers (filters) --------

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

    def _wait_confirm(
        self,
        wheel: str,
        expected_label: str,
        timeout: float = 90.0,
        poll: float = 1.0,
        min_move_time: float = 0.8,
    ) -> None:
        expected_label = (expected_label or "").strip()
        if not expected_label:
            raise azcam.exceptions.AzcamError("Expected label is empty; cannot confirm move.")

        t0 = time.time()
        saw_expected_once = False

        while True:
            cur_label = (self.indi.getfilters(retries=1).get(wheel) or "").strip()

            # Don't accept success too quickly
            if (time.time() - t0) >= min_move_time and cur_label == expected_label:
                if saw_expected_once:
                    return
                saw_expected_once = True
            else:
                saw_expected_once = False

            if (time.time() - t0) > timeout:
                raise azcam.exceptions.AzcamError(
                    f"Timeout waiting for {wheel} wheel to reach '{expected_label}'. last='{cur_label}'."
                )

            time.sleep(poll)

    # -------- required AzCam Instrument API: filters --------

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

        out: List[str] = []
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
        Filter set:
          - Requires explicit wheel prefix.
          - Short-circuits if already at requested filter.
          - Serializes moves with a lock.
          - Uses confirmation to avoid stale/instant success.
        """
        with self._move_lock:
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

            # if already in place, do not send a move command
            cur = self.indi.getfilters(retries=2)
            cur_label = (cur.get(wheel) or "").strip()
            if cur_label == expected_label:
                azcam.log(f"{wheel} wheel already at '{expected_label}' (slot {slot}); no move needed")
                return self.get_filter(filter_id=0)

            azcam.log(f"Setting {wheel} wheel to slot {slot} ('{expected_label}') from '{cur_label}'")

            # Command move
            self.indi.set_wheel_slot(wheel, slot)

            # Confirm via FILTERS telemetry
            self._wait_confirm(wheel, expected_label, timeout=90.0, poll=1.0, min_move_time=0.8)

            # Return both wheels state
            return self.get_filter(filter_id=0)

    # -------- required AzCam Instrument API: focus --------

    def get_focus(self, focus_id=0):
        """
        Return current focus position.

        focus_id currently unused (single focus mechanism).
        """
        with self._focus_lock:
            fp = self.secondary.get_focus_z(retries=3)
            self._last_focus = fp
            return fp

    def set_focus(self, focus_position, focus_id=0, focus_type="absolute"):
        """
        Move/step instrument focus.

        focus_type:
          - "absolute": set focus to focus_position
          - "step": add focus_position delta to current focus

        Returns the new focus as float.
        """
        focus_type = (focus_type or "absolute").strip().lower()
        if focus_type not in ("absolute", "step"):
            raise azcam.exceptions.AzcamError("focus_type must be 'absolute' or 'step'")

        with self._focus_lock:
            if focus_type == "absolute":
                target = float(focus_position)
            else:
                cur = self.secondary.get_focus_z(retries=3)
                delta = float(focus_position)
                target = cur + delta

            azcam.log(f"Setting focus (PosZ) to {target:.6f} ({focus_type})")

            # command move
            self.secondary.set_focus_z(target)

            # confirm
            final = self.secondary.wait_focus(
                expected=target,
                tol=0.5,
                settle_eps=0.05,
                timeout=30.0,
                poll=0.8,
            )

            self._last_focus = final
            return final

