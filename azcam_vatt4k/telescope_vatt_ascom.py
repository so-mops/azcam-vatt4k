"""
Contains the StewardAscom class which defines the Telescope Control System interface
for VATT. This version uses ASCOM/alpaca.
"""

import time
import math

from alpaca.telescope import Telescope as AlpacaTelescope
from alpaca.rotator import Rotator as AlpacaRotator

from astropy.coordinates import Angle
from astropy import units as u

import azcam
import azcam.utils
import azcam.exceptions
from azcam.tools.telescope import Telescope

from .vatt_filter_code import vatt_filters


class VattAscom(Telescope):
    """
    The interface to the VATT ASCOM telescope server.
    """

    def __init__(self, tool_id="telescope", description="VATT telescope"):
        super().__init__(tool_id, description)

        self.fits_keywords = {
            "RA": ["RightAscension", "right ascension", "str"],
            "DEC": ["Declination", "declination", "str"],
            "AIRMASS": [None, "airmass", "float"],
            "HA": [None, "hour angle", "str"],
            "LST-OBS": ["SiderealTime", "local siderial time", "str"],
            "EQUINOX": [None, "equinox of RA and DEC", "float"],
            "JULIAN": ["julianday", "julian date", "float"],
            "ELEVAT": ["Altitude", "elevation", "float"],
            "AZIMUTH": ["Azimuth", "azimuth", "float"],
            "ROTANGLE": ["Position", "rotation angle", "float"],
            "EPOCH": [None, "equinox of RA and DEC", "float"],
            "MOTION": ["Slewing", "motion flag", "int"],
            "FILTER": ["FILTER", "instrument filter", "str"],
        }
        self.vfilters = vatt_filters()

        self.host = "10.0.3.25"
        self.port = 7843

        self.tserver = AlpacaTelescope(f"{self.host}:{self.port}", 0, "http")
        self.rserver = AlpacaRotator(f"{self.host}:{self.port}", 0, "http")

        self.DEBUG = 0

        if self.verbosity:
            azcam.log(f"Connected to telescope: {self.tserver.Name}")
            azcam.log(f"Description: {self.tserver.Description}")

        if 0:
            self.initialize()

        return

    def initialize(self):
        """
        Initializes the telescope interface.
        """

        if self.is_initialized:
            return

        if not self.is_enabled:
            azcam.exceptions.warning(f"{self.description} is not enabled")
            return

        if self.verbosity:
            print(
                f"Telemetry check: RA={self.tserver.RightAscension} DE={self.tserver.Declination}"
            )

        # add keywords
        self.define_keywords()

        self.is_initialized = 1

        return

    # **************************************************************************************************
    # header
    # **************************************************************************************************
    def define_keywords(self):
        """
        Defines and resets telescope keywords.
        """

        # add keywords to header
        for key in self.fits_keywords:
            fits_list = self.fits_keywords[key]
            self.set_keyword(key, None, fits_list[1], fits_list[2])

        return

    def get_keyword(self, keyword):
        """
        Reads an telescope keyword value.
        Keyword is the name of the keyword to be read.
        This command will read hardware to obtain the keyword value.
        """

        if not self.is_enabled:
            azcam.exceptions.warning(f"{self.description} is not enabled")
            return

        try:

            if keyword == "FILTER":
                fdict = {}
                for i in range(3):
                    try:
                        fdict = self.vfilters.getfilters()
                        break
                    except Exception:
                        azcam.log(f"Filter read error {i}...")
                        time.sleep(0.2)
                        fdict["upper"] = "unknown"
                        fdict["lower"] = "unknown"
                reply = f"upper: {fdict['upper']} lower: {fdict['lower']}"

            elif keyword == "EPOCH":
                reply = 2000.0  # test

            elif keyword == "RA":
                value = getattr(self.tserver, self.fits_keywords[keyword][0])
                ra = Angle(value * u.hour)
                h = int(ra.hms.h)
                m = int(ra.hms.m)
                s = float(ra.hms.s)
                reply = f"{h:02}:{m:02}:{s:.02f}"

            elif keyword == "DEC":
                value = getattr(self.tserver, self.fits_keywords[keyword][0])
                a = Angle(value * u.deg)
                d = int(a.dms.d)
                m = abs(int(a.dms.m))
                s = abs(int(a.dms.s))
                reply = f"{d:02}:{m:02}:{s:.01f}"

            elif keyword == "AIRMASS":
                value = getattr(self.tserver, "Altitude")
                secz = 1.0 / math.cos((90.0 - value) * math.pi / 180.0)
                reply = f"{secz:.2f}"

            elif keyword == "HA":
                lst = getattr(self.tserver, self.fits_keywords["LST-OBS"][0])
                lst = lst * 24.0 / 360.0
                ra = getattr(self.tserver, self.fits_keywords["RA"][0])
                ha = Angle((lst - ra) * u.hour)
                h = int(ha.hms.h)
                m = int(ha.hms.m)
                s = float(ha.hms.s)
                reply = f"{h:02}:{m:02}:{s:.02f}"

            elif keyword == "LST-OBS":
                value = getattr(self.tserver, self.fits_keywords[keyword][0])
                a = Angle(value * u.deg)
                reply = f"{int(a.hms.h):02}:{int(a.hms.m):02}:{a.hms.s:.02f}"

            elif keyword == "EQUINOX":
                reply = 2000.0  # test

            elif keyword == "JULIAN":
                try:
                    value = self.tserver.Action("julianday", [])
                except Exception:
                    value = ""
                reply = value

            elif keyword == "ELEVAT":
                value = getattr(self.tserver, self.fits_keywords[keyword][0])
                reply = f"{value:.3f}"

            elif keyword == "MOTION":
                value = getattr(self.tserver, self.fits_keywords[keyword][0])
                reply = 1 if value else 0

            elif keyword == "AZIMUTH":
                value = getattr(self.tserver, self.fits_keywords[keyword][0])
                reply = f"{value:.4f}"

            elif keyword == "ROTANGLE":
                value = getattr(self.rserver, self.fits_keywords[keyword][0])
                reply = f"{value:.4f}"

            elif keyword == "ST":
                value = getattr(self.tserver, self.fits_keywords[keyword][0])
                a = Angle(f"{value}d")
                reply = f"{int(a.hms.h):02}:{int(a.hms.m):02}:{a.hms.s:.02f}"

            else:
                if keyword in self.fits_keywords:
                    self.header.set_keyword(
                        keyword, "unsupported", self.fits_keywords[keyword][1], "str"
                    )
                    return ["unsupported", self.fits_keywords[keyword][1], "str"]

                else:
                    raise azcam.exceptions.AzcamError(
                        f"Unknown telescope keyword: {keyword}"
                    )

            # store value in Header
            self.header.set_keyword(keyword, reply)

            reply, t = self.header.convert_type(reply, self.fits_keywords[keyword][2])

        except Exception as e:
            self._log_safe(f"header error for keyword {keyword}: {e}")
            reply = ""
            t = "str"

        return [reply, self.fits_keywords[keyword][1], t]

    # **************************************************************************************************
    # Helpers: parse RA/Dec strings for motion methods
    # **************************************************************************************************
    def _parse_ra(self, ra_str):
        """
        Convert 'HH:MM:SS' RA string to hours (float) for Alpaca.
        """
        ra_angle = Angle(ra_str, unit=u.hourangle)
        return ra_angle.hour  # hours

    def _parse_dec(self, dec_str):
        """
        Convert 'DD:MM:SS' Dec string to degrees (float).
        """
        dec_angle = Angle(dec_str, unit=u.deg)
        return dec_angle.degree  # degrees

    def _parse_angle(self, angle_str):
        """
        Generic angle string to degrees.
        """
        ang = Angle(angle_str)
        return ang.degree

    def _log_safe(self, msg):
        s = "" if msg is None else str(msg)
        azcam.log(s.replace("{", "{{").replace("}", "}}"))

    # **************************************************************************************************
    # Focus
    # **************************************************************************************************
    def set_focus(self, FocusPosition, FocusID=0, focus_type="absolute"):
        """
        Move the telescope focus to the specified position.
        Currently just prompts user to move focus and enter new focus value.
        """
        azcam.utils.prompt(f"Move to focus {FocusPosition} and press Enter...")
        self.FocusPosition = FocusPosition
        return

    def get_focus(self, FocusID=0):
        """
        Return the current telescope focus position.
        Currently just prompts user for current focus value.
        """
        focpos = azcam.utils.prompt("Enter current focus position:")

        try:
            self.FocusPosition = float(focpos)
        except Exception:
            self.FocusPosition = focpos

        return [self.FocusPosition]

    # **************************************************************************************************
    # Move – absolute RA/Dec (used by observe via 'telescope.move')
    # **************************************************************************************************
    def move(self, RA, Dec, Epoch=2000.0):
        """
        Moves telescope to an absolute RA,DEC position.
        RA, Dec are strings like 'HH:MM:SS' and 'DD:MM:SS'.
        """

        if not self.is_enabled:
            return ["WARNING", "telescope not enabled"]

        if self.DEBUG:
            azcam.log(f"DEBUG move: RA={RA}, Dec={Dec}, Epoch={Epoch}")
            return ["OK", "DEBUG"]

        ra_hours = self._parse_ra(RA)
        dec_degs = self._parse_dec(Dec)

        azcam.log(
            f"VattAscom.move: RA={RA} ({ra_hours:.6f} h), "
            f"DEC={Dec} ({dec_degs:.6f} deg), Epoch={Epoch}"
        )

        try:
            self.tserver.Tracking = True
        except Exception as e:
            self._log_safe(f"Could not set Tracking=True: {e}")

        try:
            # async slew, then we wait below
            self.tserver.SlewToCoordinatesAsync(ra_hours, dec_degs)
        except Exception as e:
            msg = f"SlewToCoordinatesAsync failed: {e}"
            self._log_safe(msg)
            return ["ERROR", msg]

        reply = self.wait_for_move()
        return reply

    def move_start(self, RA, Dec, Epoch=2000.0):
        """
        Moves telescope to an absolute RA,DEC position without waiting for motion to stop.
        Used by observe for 'move during readout' (telescope.move_start).
        """

        azcam.log(f"move_start command received: RA={RA} Dec={Dec} Epoch={Epoch}")

        if not self.is_enabled:
            azcam.exceptions.warning("telescope not enabled")
            return ["WARNING", "telescope not enabled"]

        if self.DEBUG:
            azcam.log("DEBUG move_start (no actual slew)")
            return ["OK", "DEBUG"]

        ra_hours = self._parse_ra(RA)
        dec_degs = self._parse_dec(Dec)

        try:
            self.tserver.Tracking = True
        except Exception as e:
            self._log_safe(f"Could not set Tracking=True: {e}")

        try:
            self.tserver.SlewToCoordinatesAsync(ra_hours, dec_degs)
        except Exception as e:
            msg = f"SlewToCoordinatesAsync failed in move_start: {e}"
            self._log_safe(msg)
            return ["ERROR", msg]

        # do not wait here
        return ["OK"]

    # **************************************************************************************************
    # Move – Az/Alt (for observe.azalt_mode via 'telescope.move_azalt')
    # **************************************************************************************************
    def move_azalt(self, Az, Alt):
        if not self.is_enabled:
            return ["WARNING", "telescope not enabled"]

        if self.DEBUG:
            azcam.log(f"DEBUG move_azalt: Az={Az}, Alt={Alt}")
            return ["OK", "DEBUG"]

        try:
            az_deg = float(self._parse_angle(Az))
            alt_deg = float(self._parse_angle(Alt))
        except Exception as e:
            msg = f"Bad Az/Alt values Az={Az} Alt={Alt}: {e}"
            self._log_safe(msg)
            return ["ERROR", msg]

        az_deg = az_deg % 360.0
        if alt_deg < -90.0 or alt_deg > 90.0:
            return ["ERROR", f"Alt out of range: {alt_deg}"]

        azcam.log(f"VattAscom.move_azalt: AZ={az_deg:.3f} deg, ALT={alt_deg:.3f} deg")

        try:
            self.tserver.Tracking = True
        except Exception as e:
            self._log_safe(f"Could not set Tracking=True: {e}")

        try:
            self.tserver.SlewToAltAzAsync(az_deg, alt_deg)
        except Exception as e:
            msg = f"SlewToAltAzAsync failed: {e}"
            self._log_safe(msg)
            return ["ERROR", msg]

        return self.wait_for_move()

    # **************************************************************************************************
    # Offset – small RA/Dec shifts in arcseconds (used by 'steptel')
    # **************************************************************************************************
    def offset(self, RA, Dec):
        """
        Offsets telescope in arcsecs.
        RA and Dec are arcseconds on the sky (as strings or numbers).
        Called by observe via 'telescope.offset RA_arcsec Dec_arcsec'.
        """

        if not self.is_enabled:
            return ["WARNING", "telescope not enabled"]

        if self.DEBUG:
            azcam.log(f"DEBUG offset: dRA={RA}\" dDec={Dec}\"")
            return ["OK", "DEBUG"]

        try:
            ra_arcsec = float(RA)
            dec_arcsec = float(Dec)
        except Exception as e:
            msg = f"Bad offset values RA={RA} Dec={Dec}: {e}"
            self._log_safe(msg)
            return ["ERROR", msg]

        # Current coordinates from Alpaca
        cur_ra_hours = self.tserver.RightAscension  # hours
        cur_dec_deg = self.tserver.Declination      # degrees

        cur_ra_deg = cur_ra_hours * 15.0

        # Dec offset is straightforward
        ddec_deg = dec_arcsec / 3600.0

        # RA offset given in arcsec on sky -> convert to RA coordinate degrees
        cosdec = math.cos(math.radians(cur_dec_deg)) or 1e-6
        dra_coord_deg = ra_arcsec / (3600.0 * cosdec)

        new_dec_deg = cur_dec_deg + ddec_deg
        new_ra_deg = cur_ra_deg + dra_coord_deg
        new_ra_hours = new_ra_deg / 15.0

        azcam.log(
            "VattAscom.offset: dRA=%.3f\" dDec=%.3f\" -> RA=%.6f h, DEC=%.6f deg"
            % (ra_arcsec, dec_arcsec, new_ra_hours, new_dec_deg)
        )

        try:
            self.tserver.Tracking = True
        except Exception as e:
            self._log_safe(f"Could not set Tracking=True: {e}")

        try:
            self.tserver.SlewToCoordinatesAsync(new_ra_hours, new_dec_deg)
        except Exception as e:
            msg = f"SlewToCoordinatesAsync failed in offset: {e}"
            self._log_safe(msg)
            return ["ERROR", msg]

        reply = self.wait_for_move()
        return reply

    # **************************************************************************************************
    # Wait for telescope motion to complete
    # **************************************************************************************************
    def wait_for_move(
        self,
        timeout: float = 300.0,
        start_timeout: float = 2.0,
        stop_grace: float = 1.0,
        poll: float = 0.1,
        log_every: float = 1.0,
    ):
        if not self.is_enabled:
            azcam.exceptions.warning("telescope not enabled")
            return ["WARNING", "telescope not enabled"]

        if self.DEBUG:
            azcam.log("DEBUG wait_for_move (no actual waiting)")
            return ["OK", "DEBUG"]

        def _read_slewing():
            try:
                return bool(self.tserver.Slewing), None
            except Exception as e:
                return None, e

        azcam.log("Checking for telescope motion...")

        t0 = time.time()
        last_log = 0.0

        while (time.time() - t0) < start_timeout:
            slewing, err = _read_slewing()
            if err is not None:
                msg = f"Error reading Slewing status: {err}"
                self._log_safe(msg)
                return ["ERROR", msg]
            if slewing:
                break
            time.sleep(poll)

        stop_start = None

        while True:
            elapsed = time.time() - t0
            if elapsed > timeout:
                azcam.log("Telescope motion TIMEOUT - sending AbortSlew()")
                try:
                    self.tserver.AbortSlew()
                except Exception as e:
                    self._log_safe(f"AbortSlew failed: {e}")
                return ["ERROR", f"timeout waiting for telescope motion ({timeout:.0f}s)"]

            slewing, err = _read_slewing()
            if err is not None:
                msg = f"Error reading Slewing status: {err}"
                self._log_safe(msg)
                return ["ERROR", msg]

            if (time.time() - last_log) >= log_every:
                try:
                    ra = self.get_keyword("RA")[0]
                    dec = self.get_keyword("DEC")[0]
                    azcam.log(f"Slewing={int(slewing)}  Coords: {ra} {dec}")
                except Exception:
                    azcam.log(f"Slewing={int(slewing)}  Coords: (unavailable)")
                last_log = time.time()

            if slewing:
                stop_start = None
            else:
                if stop_start is None:
                    stop_start = time.time()
                if (time.time() - stop_start) >= stop_grace:
                    azcam.log("Telescope reports it is STOPPED")
                    for _ in range(2):
                        ra = self.get_keyword("RA")[0]
                        dec = self.get_keyword("DEC")[0]
                        azcam.log(f"Final Coords: {ra} {dec}")
                    return ["OK"]

            time.sleep(poll)
