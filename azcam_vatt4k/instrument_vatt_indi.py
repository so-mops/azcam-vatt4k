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

        # These regexes are only socket read terminators / message-payload parsers.
        # XML element extraction below is handled by ElementTree, matching the
        # secondary-axis implementation pattern.
        self._filters_until_re = re.compile(
            r"<message\b"
            r"(?=[^>]*\bdevice=[\"']" + re.escape(self.QUERY_DEVICE) + r"[\"'])"
            r"(?=[^>]*\bmessage=[\"']upper:)"
            r"[^>]*>",
            re.S,
        )
        self._text_vector_done_re = re.compile(r"</(?:defTextVector|setTextVector)>")
        self._filter_message_re = re.compile(r"upper:(.+?)\s+lower:(.+)$")

    def _send_recv(self, payload: str, max_wait: float, until=None) -> str:
        return self._tcp.send_recv(payload, max_wait=max_wait, until=until)

    @staticmethod
    def _tag_name(elem: ET.Element) -> str:
        """
        Return the local XML tag name, tolerating namespaced XML if it ever appears.
        """
        tag = elem.tag
        if "}" in tag:
            return tag.rsplit("}", 1)[-1]
        return tag

    def _iter_xml_elements_wrapped(self, buf: str):
        """
        Parse a buffer containing one or more complete INDI XML stanzas by
        wrapping it in a synthetic root element.
        """
        wrapped = f"<root>{buf}</root>"
        root = ET.fromstring(wrapped)
        for elem in root.iter():
            if elem is not root:
                yield elem

    def _iter_xml_elements_pull(self, buf: str):
        """
        Streaming parse fallback for truncated buffers.

        This mirrors the secondary parser's recovery strategy: yield any
        complete XML elements already present in the response, even if the
        final stanza is incomplete.
        """
        parser = ET.XMLPullParser(events=("end",))

        try:
            parser.feed("<root>")
            parser.feed(buf)
        except ET.ParseError:
            # Keep any completed elements that the parser accepted before
            # encountering malformed or truncated trailing XML.
            pass

        try:
            for _event, elem in parser.read_events():
                if elem.tag != "root":
                    yield elem
        except ET.ParseError:
            return

        try:
            parser.feed("</root>")
            for _event, elem in parser.read_events():
                if elem.tag != "root":
                    yield elem
            parser.close()
        except ET.ParseError:
            # The buffer was truncated or otherwise incomplete; completed
            # elements have already been yielded.
            return

    def _xml_elements(self, buf: str) -> List[ET.Element]:
        """
        Return XML elements from an INDI response buffer.

        Prefer strict wrapped parsing when the response is complete. Fall back
        to XMLPullParser when the socket buffer is truncated.
        """
        if not buf:
            return []

        try:
            return list(self._iter_xml_elements_wrapped(buf))
        except ET.ParseError:
            return list(self._iter_xml_elements_pull(buf))

    def _parse_filter_message(self, message: str) -> Optional[Dict[str, str]]:
        """
        Parse the FILTERS driver's message payload.

        The payload itself is not structured XML; it is the value of the XML
        message attribute and is expected to look like:
            upper:Clear lower:U
        """
        m = self._filter_message_re.search((message or "").strip())
        if not m:
            return None

        return {
            "upper": m.group(1).strip(),
            "lower": m.group(2).strip(),
        }

    def _parse_filters_xml(self, xml: str) -> Optional[Dict[str, str]]:
        """
        Extract current in-beam filters from parsed INDI XML message elements.
        """
        match_any = None
        match_device = None

        for elem in self._xml_elements(xml):
            message = elem.attrib.get("message")
            if not message:
                continue

            parsed = self._parse_filter_message(message)
            if parsed is None:
                continue

            match_any = parsed

            dev = (elem.attrib.get("device") or "").strip()
            if dev == self.QUERY_DEVICE:
                match_device = parsed

        return match_device or match_any

    @staticmethod
    def _clean_text_value(value: Optional[str]) -> str:
        """
        Match the previous behavior for filter-name text values:
        collapse internal whitespace and strip leading/trailing whitespace.
        """
        return re.sub(r"\s+", " ", value or "").strip()

    def _parse_text_slot_element(self, elem: ET.Element) -> Optional[Tuple[int, str]]:
        """
        Parse one defText/oneText element named F0..F9 into (slot, label).
        """
        if self._tag_name(elem) not in ("defText", "oneText"):
            return None

        name = (elem.attrib.get("name") or "").strip()
        if not re.fullmatch(r"F\d", name):
            return None

        return int(name[1:]), self._clean_text_value(elem.text)

    def _parse_wheel_names_xml(self, xml: str, prop: str) -> Dict[int, str]:
        """
        Extract F-slot labels from parsed INDI text-vector XML.

        Full vector elements are preferred so device/property names can be
        checked. If the vector is truncated before its closing tag, completed
        defText/oneText child elements are still used as a fallback, matching
        the secondary parser's partial-buffer recovery model.
        """
        elements = self._xml_elements(xml)
        slotmap: Dict[int, str] = {}

        for vec in elements:
            tag = self._tag_name(vec)
            if tag not in ("defTextVector", "setTextVector"):
                continue

            dev = (vec.attrib.get("device") or "").strip()
            name = (vec.attrib.get("name") or "").strip()

            if dev and dev != self.CONTROL_DEVICE:
                continue
            if name and name != prop:
                continue

            for child in list(vec):
                parsed = self._parse_text_slot_element(child)
                if parsed is not None:
                    slot, label = parsed
                    slotmap[slot] = label

        # If the outer text vector was truncated, XMLPullParser can still
        # salvage completed child elements even though the parent vector never
        # produced an end event.
        if not slotmap:
            for elem in elements:
                parsed = self._parse_text_slot_element(elem)
                if parsed is not None:
                    slot, label = parsed
                    slotmap[slot] = label

        return slotmap

    def getfilters(self, retries: int = 3, delay: float = 0.2) -> Dict[str, str]:
        """
        Read current in-beam filter labels using QUERY_DEVICE XML message response.
        Expects message payload like: upper:Clear lower:U
        """
        last = None

        for _ in range(max(1, retries)):
            xml = self._send_recv(
                f"<getProperties version='1.7' device='{self.QUERY_DEVICE}' />",
                max_wait=max(1.5, self.cfg.timeout),
                until=self._filters_until_re,
            )
            last = xml

            parsed = self._parse_filters_xml(xml)
            if parsed is not None:
                return parsed

            time.sleep(delay)

        raise RuntimeError(f"Could not parse FILTERS XML response: {last!r}")

    def get_wheel_names(self, wheel: str) -> Dict[int, str]:
        """
        Returns slot->label for wheel ('upper' or 'lower') by parsing the
        text vector property LOWER_FNAMES / UPPER_FNAMES as XML.
        """
        wheel = wheel.lower().strip()
        if wheel not in ("upper", "lower"):
            raise ValueError("wheel must be 'upper' or 'lower'")

        prop = self.UPPER_NAMES_PROP if wheel == "upper" else self.LOWER_NAMES_PROP

        xml = self._send_recv(
            f"<getProperties version='1.7' device='{self.CONTROL_DEVICE}' name='{prop}' />",
            max_wait=max(1.5, self.cfg.timeout),
            until=self._text_vector_done_re,
        )

        slotmap = self._parse_wheel_names_xml(xml, prop)

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
# Secondary
# =========================

class VattIndiSecondary:
    """
    Minimal polling-style INDI client for VATT Secondary axes.

    Supported logical axes:
      - focus -> PosZ / Z
      - tiltx -> PosV / V
      - tilty -> PosU / U

    Protocol:
      - read a short buffer from INDI
      - parse XML elements by wrapping in a fake root
      - if buffer is truncated, use XMLPullParser to salvage complete stanzas
      - pick the latest setNumberVector (preferred) or defNumberVector (fallback)
      - extract axis value + vector state
    """

    DEVICE = "VATT Secondary"

    AXES = {
        "focus": {"prop": "PosZ", "elem": "Z"},
        "tiltx": {"prop": "PosV", "elem": "V"},
        "tilty": {"prop": "PosU", "elem": "U"},
    }

    def __init__(self, cfg: Optional[IndiConfig] = None):
        self.cfg = cfg or IndiConfig()
        self._tcp = IndiTcpClient(self.cfg)

    def _axis_spec(self, axis: str) -> Dict[str, str]:
        axis = (axis or "").strip().lower()
        if axis not in self.AXES:
            raise ValueError(f"Unknown secondary axis '{axis}'. Expected one of {list(self.AXES)}")
        return self.AXES[axis]

    def _read_axis_buffer(self, axis: str, max_wait: Optional[float] = None) -> str:
        if max_wait is None:
            max_wait = max(1.6, getattr(self.cfg, "timeout", 1.0))

        spec = self._axis_spec(axis)
        return self._tcp.send_recv(
            f"<getProperties version='1.7' device='{self.DEVICE}' name='{spec['prop']}' />",
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

    def _select_latest_axis_vector(self, axis: str, buf: str) -> ET.Element:
        if not buf:
            raise RuntimeError(f"Empty INDI response buffer for secondary axis '{axis}'.")

        spec = self._axis_spec(axis)
        prop = spec["prop"]

        candidates_set = []
        candidates_def = []

        try:
            elements = list(self._iter_vector_elements_wrapped(buf))
        except ET.ParseError:
            elements = list(self._iter_vector_elements_pull(buf))

        for elem in elements:
            dev = (elem.attrib.get("device") or "").strip()
            name = (elem.attrib.get("name") or "").strip()
            if dev != self.DEVICE or name != prop:
                continue

            if elem.tag == "setNumberVector":
                candidates_set.append(elem)
            else:
                candidates_def.append(elem)

        if candidates_set:
            return candidates_set[-1]
        if candidates_def:
            return candidates_def[-1]

        raise RuntimeError(f"No {self.DEVICE}.{prop} NumberVector found in buffer for axis '{axis}'.")

    def _parse_axis_vector(self, axis: str, vec: ET.Element) -> Tuple[float, str]:
        spec = self._axis_spec(axis)
        elem_name = spec["elem"]

        state = (vec.attrib.get("state") or "").strip()

        value_text = None
        for child in vec.findall(".//oneNumber") + vec.findall(".//defNumber"):
            if (child.attrib.get("name") or "") == elem_name:
                if child.text is not None:
                    value_text = child.text.strip()
                break

        if value_text is None:
            raise RuntimeError(f"{spec['prop']} vector missing {elem_name} value for axis '{axis}'.")

        try:
            return float(value_text), state
        except ValueError:
            raise RuntimeError(
                f"{spec['prop']} {elem_name} value not numeric for axis '{axis}': {value_text!r}"
            )

    def get_axis_and_state(self, axis: str, retries: int = 3, delay: float = 0.15) -> Tuple[float, str]:
        last_buf = None
        for _ in range(max(1, retries)):
            try:
                last_buf = self._read_axis_buffer(axis)
                vec = self._select_latest_axis_vector(axis, last_buf)
                return self._parse_axis_vector(axis, vec)
            except Exception:
                time.sleep(delay)

        spec = self._axis_spec(axis)
        raise RuntimeError(
            f"Could not read/parse {self.DEVICE}.{spec['prop']} for axis '{axis}'. "
            f"last_buf_tail={(last_buf or '')[-500:]!r}"
        )

    def get_axis(self, axis: str, retries: int = 3, delay: float = 0.15) -> float:
        value, _state = self.get_axis_and_state(axis, retries=retries, delay=delay)
        return value

    def set_axis(self, axis: str, value: float) -> None:
        spec = self._axis_spec(axis)
        v = float(value)
        xml = (
            f"<newNumberVector device='{self.DEVICE}' name='{spec['prop']}'>"
            f"<oneNumber name='{spec['elem']}'>{v}</oneNumber>"
            f"</newNumberVector>"
        )
        self._tcp.send_recv(xml, max_wait=max(0.5, getattr(self.cfg, "timeout", 1.0)), until=None)

    def wait_axis(
        self,
        axis: str,
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
            cur, state = self.get_axis_and_state(axis, retries=2, delay=0.1)
            state_norm = (state or "").strip().lower()

            if state_norm == "alert":
                raise RuntimeError(f"Secondary axis '{axis}' entered ALERT state (last={cur})")

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
                raise RuntimeError(
                    f"Timeout waiting for secondary axis '{axis}' to reach {expected}. "
                    f"last={cur} state={state}"
                )

            time.sleep(poll)

    def get_focus_z_and_state(self, retries: int = 3, delay: float = 0.15) -> Tuple[float, str]:
        return self.get_axis_and_state("focus", retries=retries, delay=delay)

    def get_focus_z(self, retries: int = 3, delay: float = 0.15) -> float:
        return self.get_axis("focus", retries=retries, delay=delay)

    def set_focus_z(self, value: float) -> None:
        self.set_axis("focus", value)

    def wait_focus(
        self,
        expected: float,
        tol: float = 0.5,
        settle_eps: float = 0.05,
        timeout: float = 30.0,
        poll: float = 0.8,
        min_wait: float = 1.2,
    ) -> float:
        return self.wait_axis(
            "focus",
            expected=expected,
            tol=tol,
            settle_eps=settle_eps,
            timeout=timeout,
            poll=poll,
            min_wait=min_wait,
        )


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
        
        self._secondary_lock = threading.Lock()

        # keep local cached last values for secondary axes
        self._last_secondary: Dict[str, float] = {}

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

    # -------- internal helpers (secondary axes) --------

    def _validate_focus_type(self, focus_type: str) -> str:
        focus_type = (focus_type or "absolute").strip().lower()
        if focus_type not in ("absolute", "step"):
            raise azcam.exceptions.AzcamError("focus_type must be 'absolute' or 'step'")
        return focus_type

    def _get_secondary_axis(self, axis: str) -> float:
        with self._secondary_lock:
            value = self.secondary.get_axis(axis, retries=3)
            self._last_secondary[axis] = value
            return value

    def _set_secondary_axis(self, axis: str, position, focus_type="absolute") -> float:
        focus_type = self._validate_focus_type(focus_type)

        with self._secondary_lock:
            if focus_type == "absolute":
                target = float(position)
            else:
                cur = self.secondary.get_axis(axis, retries=3)
                delta = float(position)
                target = cur + delta

            azcam.log(f"Setting secondary axis '{axis}' to {target:.6f} ({focus_type})")

            self.secondary.set_axis(axis, target)

            final = self.secondary.wait_axis(
                axis=axis,
                expected=target,
                tol=0.5,
                settle_eps=0.05,
                timeout=30.0,
                poll=0.8,
            )

            self._last_secondary[axis] = final
            return final

    # -------- required AzCam Instrument API: focus --------

    def get_focus(self, focus_id=0):
        """
        Return current focus position.

        focus_id currently unused (single focus mechanism).
        """
        return self._get_secondary_axis("focus")

    def set_focus(self, focus_position, focus_id=0, focus_type="absolute"):
        """
        Move/step instrument focus.

        focus_type:
          - "absolute": set focus to focus_position
          - "step": add focus_position delta to current focus

        Returns the new focus as float.
        """
        return self._set_secondary_axis("focus", focus_position, focus_type=focus_type)

    # -------- additional AzCam Instrument API: tilt --------

    def get_tiltx(self):
        """
        Return current tilt X position.
        """
        return self._get_secondary_axis("tiltx")

    def set_tiltx(self, tilt_position, focus_type="absolute"):
        """
        Move/step instrument tilt X.

        focus_type:
          - "absolute": set tilt X to tilt_position
          - "step": add tilt_position delta to current tilt X

        Returns the new tilt X as float.
        """
        return self._set_secondary_axis("tiltx", tilt_position, focus_type=focus_type)

    def get_tilty(self):
        """
        Return current tilt Y position.
        """
        return self._get_secondary_axis("tilty")

    def set_tilty(self, tilt_position, focus_type="absolute"):
        """
        Move/step instrument tilt Y.

        focus_type:
          - "absolute": set tilt Y to tilt_position
          - "step": add tilt_position delta to current tilt Y

        Returns the new tilt Y as float.
        """
        return self._set_secondary_axis("tilty", tilt_position, focus_type=focus_type)