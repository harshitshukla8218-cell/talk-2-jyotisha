import hashlib
import json
import math
from html import escape
from datetime import date, datetime, time, timezone
from importlib.metadata import PackageNotFoundError, distribution
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SIGNS = (
    "Mesha (Aries)",
    "Vrishabha (Taurus)",
    "Mithuna (Gemini)",
    "Karka (Cancer)",
    "Simha (Leo)",
    "Kanya (Virgo)",
    "Tula (Libra)",
    "Vrishchika (Scorpio)",
    "Dhanu (Sagittarius)",
    "Makara (Capricorn)",
    "Kumbha (Aquarius)",
    "Meena (Pisces)",
)
NAKSHATRAS = (
    "Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashirsha", "Ardra",
    "Punarvasu", "Pushya", "Ashlesha", "Magha", "Purva Phalguni",
    "Uttara Phalguni", "Hasta", "Chitra", "Swati", "Vishakha", "Anuradha",
    "Jyeshtha", "Mula", "Purva Ashadha", "Uttara Ashadha", "Shravana",
    "Dhanishtha", "Shatabhisha", "Purva Bhadrapada", "Uttara Bhadrapada",
    "Revati",
)
PLANETS = (
    ("Sun", "sun"),
    ("Moon", "moon"),
    ("Mercury", "mercury"),
    ("Venus", "venus"),
    ("Mars", "mars"),
    ("Jupiter", "jupiter barycenter"),
    ("Saturn", "saturn barycenter"),
    ("Uranus", "uranus barycenter"),
    ("Neptune", "neptune barycenter"),
    ("Pluto", "pluto barycenter"),
)
CHART_SETUP_MESSAGE = (
    "Birth-chart generation needs Skyfield, its bundled JPL ephemeris, and a "
    "configured birthplace geocoder. Install project requirements and set "
    "T2J_GEOCODER_USER_AGENT before trying again."
)
REPORT_SETUP_MESSAGE = "PDF reports need the ReportLab package. Install project requirements and try again."


class ChartConfigurationError(RuntimeError):
    pass


class ChartInputError(ValueError):
    pass


def profile_fingerprint(details):
    source = {
        key: details.get(key)
        for key in (
            "fullName",
            "dateOfBirth",
            "timeOfBirth",
            "birthTimeUnknown",
            "birthTimeAccuracy",
            "placeOfBirth",
            "region",
            "country",
        )
    }
    return hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def geocode_birthplace(details, user_agent):
    if not user_agent:
        raise ChartConfigurationError(CHART_SETUP_MESSAGE)
    try:
        from geopy.exc import GeocoderServiceError
        from geopy.extra.rate_limiter import RateLimiter
        from geopy.geocoders import Nominatim
        from tzfpy import get_tz
    except ImportError as error:
        raise ChartConfigurationError(CHART_SETUP_MESSAGE) from error

    query = ", ".join(
        part
        for part in (
            details.get("placeOfBirth"),
            details.get("region"),
            details.get("country"),
        )
        if part
    )
    try:
        geocoder = Nominatim(user_agent=user_agent, timeout=10)
        geocode = RateLimiter(
            geocoder.geocode,
            min_delay_seconds=1,
            swallow_exceptions=False,
        )
        location = geocode(
            query,
            exactly_one=True,
            addressdetails=False,
        )
    except (GeocoderServiceError, TimeoutError, OSError) as error:
        raise ChartInputError(
            "The birthplace could not be looked up right now. Please try again later."
        ) from error
    if location is None:
        raise ChartInputError(
            "The birthplace could not be found. Check the city, state, and country in your profile."
        )
    timezone_name = get_tz(float(location.longitude), float(location.latitude))
    if not timezone_name:
        raise ChartInputError(
            "A time zone could not be resolved for this birthplace."
        )
    return {
        "latitude": float(location.latitude),
        "longitude": float(location.longitude),
        "timezone": timezone_name,
    }


def _birth_time_utc(details, timezone_name):
    if details.get("birthTimeUnknown") or not details.get("timeOfBirth"):
        raise ChartInputError(
            "Add your exact time of birth to calculate an Ascendant and houses."
        )
    if details.get("birthTimeAccuracy") != "exact":
        raise ChartInputError(
            "Set birth-time accuracy to Exact before generating a chart."
        )
    try:
        birth_date = date.fromisoformat(details["dateOfBirth"])
        birth_time = time.fromisoformat(details["timeOfBirth"])
        birth_timezone = ZoneInfo(timezone_name)
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as error:
        raise ChartInputError("The saved birth date, time, or time zone is invalid.") from error

    local_birth = datetime.combine(birth_date, birth_time)
    candidates = []
    for fold in (0, 1):
        candidate = local_birth.replace(tzinfo=birth_timezone, fold=fold)
        utc_candidate = candidate.astimezone(timezone.utc)
        round_trip = utc_candidate.astimezone(birth_timezone).replace(tzinfo=None)
        if round_trip == local_birth and utc_candidate not in candidates:
            candidates.append(utc_candidate)
    if not candidates:
        raise ChartInputError(
            "That local birth time did not occur because of a daylight-saving clock change."
        )
    if len(candidates) > 1:
        raise ChartInputError(
            "That local birth time occurred twice because of a daylight-saving clock change. "
            "This profile cannot distinguish the two times."
        )
    if not date(1900, 1, 1) <= birth_date <= date(2050, 12, 31):
        raise ChartInputError(
            "The bundled JPL ephemeris supports birth dates from 1900 through 2050."
        )
    return candidates[0]


def _lahiri_ayanamsa(julian_tt):
    years = (julian_tt - 2451545.0) / 365.2425
    return (
        23.0
        + 51.0 / 60.0
        + 11.2 / 3600.0
        + 50.2719 * years / 3600.0
    )


def _sign(longitude):
    return int(longitude // 30) % 12


def _navamsa_sign(longitude):
    sign_index = _sign(longitude)
    sign_type = sign_index % 3
    start = sign_index + (0 if sign_type == 0 else 8 if sign_type == 1 else 4)
    division = int((longitude % 30) / (30 / 9))
    return (start + min(division, 8)) % 12


def _ascendant(longitude, latitude, skyfield_time, ayanamsa):
    obliquity = math.radians(
        23.43929111
        - 0.013004167 * ((skyfield_time.tt - 2451545.0) / 36525.0)
    )
    sidereal_angle = math.radians(
        (float(skyfield_time.gast) * 15 + longitude) % 360
    )
    geographic_latitude = math.radians(latitude)
    coefficient_cos = math.cos(geographic_latitude) * math.cos(sidereal_angle)
    coefficient_sin = (
        math.cos(geographic_latitude)
        * math.sin(sidereal_angle)
        * math.cos(obliquity)
        + math.sin(geographic_latitude) * math.sin(obliquity)
    )
    candidate = math.degrees(math.atan2(-coefficient_cos, coefficient_sin)) % 360
    tropical_candidates = (candidate, (candidate + 180) % 360)
    eastern_candidate = None
    for value in tropical_candidates:
        right_ascension = math.degrees(
            math.atan2(
                math.sin(math.radians(value)) * math.cos(obliquity),
                math.cos(math.radians(value)),
            )
        ) % 360
        eastward_hour_angle = (right_ascension - math.degrees(sidereal_angle)) % 360
        if 0 < eastward_hour_angle < 180:
            eastern_candidate = value
            break
    if eastern_candidate is None:
        raise ChartInputError("The Ascendant could not be calculated for this birth location.")
    return float((eastern_candidate - ayanamsa) % 360)


def calculate_birth_chart(details, location):
    try:
        from skyfield.api import load_file, load
        ephemeris_path = distribution("skyfield-data").locate_file(
            "skyfield_data/data/de421.bsp"
        )
    except PackageNotFoundError as error:
        raise ChartConfigurationError(CHART_SETUP_MESSAGE) from error
    except ImportError as error:
        raise ChartConfigurationError(CHART_SETUP_MESSAGE) from error

    utc_birth = _birth_time_utc(details, location["timezone"])
    if not ephemeris_path.is_file():
        raise ChartConfigurationError(CHART_SETUP_MESSAGE)
    try:
        ephemeris = load_file(str(ephemeris_path))
        timescale = load.timescale(builtin=True)
        skyfield_time = timescale.from_datetime(utc_birth)
        ayanamsa = _lahiri_ayanamsa(skyfield_time.tt)
        earth = ephemeris["earth"]
        positions = []
        for display_name, body_name in PLANETS:
            apparent = earth.at(skyfield_time).observe(
                ephemeris[body_name]
            ).apparent()
            _, ecliptic_longitude, _ = apparent.ecliptic_latlon(epoch="date")
            sidereal_longitude = (float(ecliptic_longitude.degrees) - ayanamsa) % 360
            sign_index = _sign(sidereal_longitude)
            positions.append(
                {
                    "name": display_name,
                    "longitude": round(sidereal_longitude, 4),
                    "signIndex": sign_index,
                    "sign": SIGNS[sign_index],
                    "degreeInSign": round(sidereal_longitude % 30, 2),
                    "navamsaSignIndex": _navamsa_sign(sidereal_longitude),
                    "navamsaSign": SIGNS[_navamsa_sign(sidereal_longitude)],
                }
            )
    except (KeyError, ValueError, OSError, RuntimeError) as error:
        raise ChartConfigurationError(
            "The bundled JPL ephemeris could not calculate this chart. "
            "Check the installed project requirements."
        ) from error

    julian_centuries = (skyfield_time.tt - 2451545.0) / 36525.0
    mean_node_tropical = (
        125.04452
        - 1934.136261 * julian_centuries
        + 0.0020708 * julian_centuries**2
        + julian_centuries**3 / 450000.0
    ) % 360
    for name, tropical_longitude in (
        ("Rahu (mean node)", mean_node_tropical),
        ("Ketu (mean node)", (mean_node_tropical + 180) % 360),
    ):
        sidereal_longitude = (tropical_longitude - ayanamsa) % 360
        sign_index = _sign(sidereal_longitude)
        positions.append(
            {
                "name": name,
                "longitude": round(sidereal_longitude, 4),
                "signIndex": sign_index,
                "sign": SIGNS[sign_index],
                "degreeInSign": round(sidereal_longitude % 30, 2),
                "navamsaSignIndex": _navamsa_sign(sidereal_longitude),
                "navamsaSign": SIGNS[_navamsa_sign(sidereal_longitude)],
                "calculation": "mean lunar node",
            }
        )

    ascendant_longitude = _ascendant(
        location["longitude"],
        location["latitude"],
        skyfield_time,
        ayanamsa,
    )
    ascendant_sign_index = _sign(ascendant_longitude)
    ascendant = {
        "longitude": round(ascendant_longitude, 4),
        "signIndex": ascendant_sign_index,
        "sign": SIGNS[ascendant_sign_index],
        "degreeInSign": round(ascendant_longitude % 30, 2),
        "navamsaSignIndex": _navamsa_sign(ascendant_longitude),
        "navamsaSign": SIGNS[_navamsa_sign(ascendant_longitude)],
    }
    for planet in positions:
        planet["house"] = (
            planet["signIndex"] - ascendant_sign_index
        ) % 12 + 1
        planet["navamsaHouse"] = (
            planet["navamsaSignIndex"] - ascendant["navamsaSignIndex"]
        ) % 12 + 1

    moon = next(planet for planet in positions if planet["name"] == "Moon")
    nakshatra_width = 360 / 27
    nakshatra_index = int(moon["longitude"] / nakshatra_width)
    pada = int((moon["longitude"] % nakshatra_width) / (nakshatra_width / 4)) + 1
    houses = []
    navamsa_houses = []
    for house_number in range(1, 13):
        sign_index = (ascendant_sign_index + house_number - 1) % 12
        navamsa_sign_index = (
            ascendant["navamsaSignIndex"] + house_number - 1
        ) % 12
        houses.append(
            {
                "house": house_number,
                "signIndex": sign_index,
                "sign": SIGNS[sign_index],
                "planets": [
                    planet["name"] for planet in positions
                    if planet["house"] == house_number
                ],
            }
        )
        navamsa_houses.append(
            {
                "house": house_number,
                "signIndex": navamsa_sign_index,
                "sign": SIGNS[navamsa_sign_index],
                "planets": [
                    planet["name"] for planet in positions
                    if planet["navamsaHouse"] == house_number
                ],
            }
        )
    return {
        "engine": "Skyfield JPL DE421 with Lahiri sidereal conversion",
        "ayanamsa": "Lahiri",
        "ayanamsaDegrees": round(ayanamsa, 6),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "birthTimeUtc": utc_birth.isoformat(),
        "location": {
            "city": details["placeOfBirth"],
            "state": details["region"],
            "country": details["country"],
            "timezone": location["timezone"],
        },
        "profileSnapshot": {
            "dateOfBirth": details["dateOfBirth"],
            "timeOfBirth": details["timeOfBirth"],
            "placeOfBirth": details["placeOfBirth"],
            "region": details["region"],
            "country": details["country"],
        },
        "ascendant": ascendant,
        "moonSign": moon["sign"],
        "sunSign": next(planet["sign"] for planet in positions if planet["name"] == "Sun"),
        "nakshatra": {
            "name": NAKSHATRAS[nakshatra_index],
            "pada": pada,
        },
        "planets": positions,
        "houses": houses,
        "navamsa": {
            "ascendant": ascendant["navamsaSign"],
            "houses": navamsa_houses,
        },
        "interpretationAvailable": False,
        "interpretationMessage": (
            "AI interpretation is not configured. Calculated positions are shown without "
            "AI-generated personality or life predictions."
        ),
        "disclaimer": (
            "Astrology interpretations are for informational purposes only. This chart "
            "uses an approximate Lahiri ayanamsa and whole-sign Vedic houses."
        ),
    }


def render_chart_pdf(details, chart):
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.graphics.shapes import Drawing, Line, Rect, String
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as error:
        raise ChartConfigurationError(REPORT_SETUP_MESSAGE) from error

    from io import BytesIO

    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="Talk2Jyotisha Birth Chart",
        author="Talk2Jyotisha",
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph("Talk2Jyotisha - Vedic Birth Chart", styles["Title"]),
        Spacer(1, 5 * mm),
        Paragraph(
            f"Name: {escape(details['fullName'])}<br/>"
            f"Birth: {escape(details['dateOfBirth'])} {escape(details['timeOfBirth'])} "
            f"({escape(chart['location']['timezone'])})<br/>"
            f"Place: {escape(chart['location']['city'])}, "
            f"{escape(chart['location']['state'])}, "
            f"{escape(chart['location']['country'])}<br/>"
            f"Ascendant: {escape(chart['ascendant']['sign'])}<br/>"
            f"Moon sign: {escape(chart['moonSign'])}<br/>"
            f"Nakshatra: {escape(chart['nakshatra']['name'])} "
            f"(Pada {chart['nakshatra']['pada']})",
            styles["BodyText"],
        ),
        Spacer(1, 5 * mm),
        Paragraph("Rashi chart (D1)", styles["Heading2"]),
    ]
    chart_drawing = Drawing(300, 300)
    chart_drawing.add(Rect(5, 5, 290, 290, fillColor=None, strokeColor=colors.HexColor("#9a7a3d")))
    chart_drawing.add(Line(5, 5, 295, 295, strokeColor=colors.HexColor("#9a7a3d")))
    chart_drawing.add(Line(295, 5, 5, 295, strokeColor=colors.HexColor("#9a7a3d")))
    chart_drawing.add(Line(150, 5, 295, 150, strokeColor=colors.HexColor("#9a7a3d")))
    chart_drawing.add(Line(295, 150, 150, 295, strokeColor=colors.HexColor("#9a7a3d")))
    chart_drawing.add(Line(150, 295, 5, 150, strokeColor=colors.HexColor("#9a7a3d")))
    chart_drawing.add(Line(5, 150, 150, 5, strokeColor=colors.HexColor("#9a7a3d")))
    chart_centers = (
        (150, 244), (82, 244), (52, 200), (52, 150), (52, 100), (82, 56),
        (150, 56), (218, 56), (248, 100), (248, 150), (248, 200), (218, 244),
    )
    for house, (center_x, center_y) in zip(chart["houses"], chart_centers):
        chart_drawing.add(
            String(
                center_x,
                center_y + 12,
                f"H{house['house']} {house['sign'].split(' ')[0]}",
                fontName="Helvetica-Bold",
                fontSize=7,
                textAnchor="middle",
                fillColor=colors.HexColor("#302719"),
            )
        )
        chart_drawing.add(
            String(
                center_x,
                center_y,
                ", ".join(house["planets"])[:32],
                fontName="Helvetica",
                fontSize=6,
                textAnchor="middle",
                fillColor=colors.HexColor("#302719"),
            )
        )
    story.extend(
        [
            chart_drawing,
            Spacer(1, 4 * mm),
            Paragraph("Planetary positions (Lahiri sidereal zodiac)", styles["Heading2"]),
        ]
    )
    rows = [["Planet", "Sign", "Degree", "House"]]
    rows.extend(
        [
            planet["name"],
            planet["sign"],
            f"{planet['degreeInSign']:.2f} deg",
            str(planet["house"]),
        ]
        for planet in chart["planets"]
    )
    table = Table(rows, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#302719")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#f0d18a")),
                ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#29251c")),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cdbb8c")),
                ("PAD", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.extend(
        [
            table,
            Spacer(1, 5 * mm),
            Paragraph("Navamsa (D9) house placements", styles["Heading2"]),
            Paragraph(
                "<br/>".join(
                    f"House {house['house']} - {escape(house['sign'])}: "
                    f"{escape(', '.join(house['planets']) or 'No planets')}"
                    for house in chart["navamsa"]["houses"]
                ),
                styles["BodyText"],
            ),
            Spacer(1, 5 * mm),
            Paragraph("Calculation notes", styles["Heading2"]),
            Paragraph(
                "Ephemeris: JPL DE421 via Skyfield. Zodiac: Lahiri sidereal "
                "(approximate ayanamsa). Houses: whole-sign. Rahu/Ketu use the mean "
                "lunar node. Navamsa uses the standard sign-based D9 division.",
                styles["BodyText"],
            ),
            Spacer(1, 3 * mm),
            Paragraph(chart["disclaimer"], styles["BodyText"]),
        ]
    )
    document.build(story)
    return output.getvalue()
