# Copyright (c) 2026, Beveren Software and contributors
# For license information, please see license.txt

import datetime
import json

import frappe
from frappe import _


# ---------------------------------------------------------------------------
# ICS feed endpoint — allow_guest so calendar apps can subscribe via URL
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def get_feed(token):
	"""Return an ICS calendar feed identified by its token.

	The token is the only authentication — treat subscription URLs as secrets.
	"""
	if not token or not isinstance(token, str) or len(token) > 128:
		frappe.throw(_("Invalid token"), frappe.ValidationError)

	# Fetch feed config directly via DB to avoid permission checks on the
	# iCal Feed doctype for guest requests — the token IS the credential.
	feed = frappe.db.get_value(
		"iCal Feed",
		{"token": token, "enabled": 1},
		[
			"name",
			"title",
			"doctype_name",
			"field_start",
			"field_end",
			"field_summary",
			"field_summary_subfield",
			"field_description",
			"field_description_subfield",
			"field_location",
			"field_location_subfield",
			"filters_json",
		],
		as_dict=True,
	)
	if not feed:
		frappe.throw(_("Feed not found or disabled"), frappe.DoesNotExistError)

	# --- Validate doctype and fields ----------------------------------------
	if not frappe.db.exists("DocType", feed.doctype_name):
		frappe.throw(_("DocType not found"), frappe.ValidationError)

	meta = frappe.get_meta(feed.doctype_name)
	valid_fields = {f.fieldname for f in meta.fields} | {"name", "creation", "modified", "owner"}

	# Build a map of fieldname -> options (doctype it links to) for link fields
	field_link_targets = {
		f.fieldname: (f.options or "")
		for f in meta.fields
		if f.fieldtype == "Link"
	}

	for fname in (feed.field_start, feed.field_end, feed.field_summary, feed.field_description, feed.field_location):
		if fname and fname not in valid_fields:
			frappe.throw(_("Invalid field: {0}").format(fname), frappe.ValidationError)

	# Validate subfields against their linked doctypes
	for parent_fname, subfield_fname in (
		(feed.field_summary, feed.field_summary_subfield),
		(feed.field_description, feed.field_description_subfield),
		(feed.field_location, feed.field_location_subfield),
	):
		if parent_fname and subfield_fname:
			parent_meta = next((f for f in meta.fields if f.fieldname == parent_fname), None)
			if parent_meta and parent_meta.fieldtype == "Link" and parent_meta.options:
				linked_meta = frappe.get_meta(parent_meta.options)
				linked_valid = {f.fieldname for f in linked_meta.fields} | {"name"}
				if subfield_fname not in linked_valid:
					frappe.throw(_("Invalid subfield: {0}").format(subfield_fname), frappe.ValidationError)

	# --- Parse filters -------------------------------------------------------
	filters = []
	if feed.filters_json:
		try:
			filters = json.loads(feed.filters_json)
		except (json.JSONDecodeError, ValueError):
			filters = []

	# --- Fetch records -------------------------------------------------------
	fetch_fields = list({feed.field_start, feed.field_end, "name"})
	for f in (feed.field_summary, feed.field_description, feed.field_location):
		if f:
			fetch_fields.append(f)
	fetch_fields = list(set(fetch_fields))

	records = frappe.get_list(
		feed.doctype_name,
		filters=filters,
		fields=fetch_fields,
		limit_page_length=5000,
		ignore_permissions=True,  # token is the authorization
	)

	# --- Build ICS -----------------------------------------------------------
	try:
		from icalendar import Calendar, Event
	except ImportError:
		frappe.throw(_("icalendar package not installed. Run: pip install icalendar"))

	try:
		import pytz
	except ImportError:
		frappe.throw(_("pytz package not installed. Run: pip install pytz"))

	system_tz_name = frappe.db.get_single_value("System Settings", "time_zone") or "UTC"
	try:
		tz = pytz.timezone(system_tz_name)
	except Exception:
		tz = pytz.UTC

	cal = Calendar()
	cal.add("prodid", f"-//Frappe iCal Feed//{feed.doctype_name}//EN")
	cal.add("version", "2.0")
	cal.add("calscale", "GREGORIAN")
	cal.add("method", "PUBLISH")
	cal.add("X-WR-CALNAME", feed.title or feed.doctype_name)
	cal.add("X-WR-TIMEZONE", system_tz_name)
	cal.add("X-WR-CALDESC", f"Frappe {feed.doctype_name}")
	cal.add("REFRESH-INTERVAL;VALUE=DURATION", "PT1H")

	site_url = frappe.utils.get_url()

	for r in records:
		start_val = r.get(feed.field_start)
		if not start_val:
			continue

		start_dt = _to_aware_datetime(start_val, tz)
		if not start_dt:
			continue

		end_val = r.get(feed.field_end) if feed.field_end else None
		end_dt = _to_aware_datetime(end_val, tz) if end_val else None
		if not end_dt or end_dt <= start_dt:
			end_dt = start_dt + datetime.timedelta(hours=1)

		summary_field = feed.field_summary or "name"
		raw_summary = r.get(summary_field) or r.get("name") or ""
		summary = _resolve_subfield_value(
			raw_summary, summary_field, feed.field_summary_subfield, field_link_targets
		) if feed.field_summary_subfield else str(raw_summary)

		event = Event()
		event.add("uid", f"{feed.name}-{r.name}@frappe-ical")
		event.add("summary", summary)
		event.add("dtstart", start_dt)
		event.add("dtend", end_dt)
		event.add("url", f"{site_url}/app/{frappe.scrub(feed.doctype_name)}/{r.name}")

		if feed.field_description:
			desc_raw = r.get(feed.field_description)
			if desc_raw:
				if feed.field_description_subfield:
					desc = _resolve_subfield_value(
						str(desc_raw), feed.field_description,
						feed.field_description_subfield, field_link_targets
					)
				else:
					desc = str(desc_raw)
				if desc:
					event.add("description", desc)

		if feed.field_location:
			loc = r.get(feed.field_location)
			if loc:
				if feed.field_location_subfield:
					# User picked a specific subfield — use it as plain text, no geo
					loc_text = _resolve_subfield_value(
						str(loc), feed.field_location,
						feed.field_location_subfield, field_link_targets
					)
					if loc_text:
						event.add("location", loc_text)
				else:
					loc_result = _resolve_location(str(loc), feed.field_location, field_link_targets)
					if loc_result["text"]:
						event.add("location", loc_result["text"])
						if loc_result["lat"] is not None and loc_result["lng"] is not None:
							lat, lng = loc_result["lat"], loc_result["lng"]
							# RFC 5545 GEO property
							from icalendar import vGeo
							event.add("geo", vGeo((lat, lng)))
							# Apple Maps integration
							from icalendar import vText as _vText
							apple_loc = _vText(f"geo:{lat},{lng}")
							apple_loc.params["X-TITLE"] = loc_result["text"]
							event.add("X-APPLE-STRUCTURED-LOCATION", apple_loc)

		cal.add_component(event)

	ics_bytes = cal.to_ical()

	frappe.response["type"] = "download"
	frappe.response["filename"] = f"{frappe.scrub(feed.title or feed.name)}.ics"
	frappe.response["filecontent"] = ics_bytes
	frappe.response["content_type"] = "text/calendar; charset=utf-8"
	frappe.response["display_content_as"] = "inline"


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

@frappe.whitelist()
def regenerate_token(feed_name):
	"""Regenerate the token for an iCal Feed. Requires write permission."""
	if not frappe.has_permission("iCal Feed", "write", doc=feed_name, throw=False):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	new_token = frappe.generate_hash(length=32)
	frappe.db.set_value("iCal Feed", feed_name, "token", new_token)
	frappe.db.commit()
	return {"token": new_token}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_aware_datetime(val, tz):
	"""Convert a datetime value (string or object) to a timezone-aware datetime."""
	if not val:
		return None
	try:
		import pytz

		if isinstance(val, datetime.datetime):
			return tz.localize(val) if val.tzinfo is None else val
		if isinstance(val, str):
			dt = frappe.utils.get_datetime(val)
			if dt:
				return tz.localize(dt) if dt.tzinfo is None else dt
	except Exception:
		pass
	return None


def _resolve_subfield_value(link_value, fieldname, subfield, field_link_targets):
	"""Fetch a specific subfield from a linked record.

	Returns the subfield value as a plain string, HTML-stripped.
	Falls back to the original link_value if the lookup fails.
	"""
	if not link_value or not subfield:
		return link_value or ""

	linked_doctype = field_link_targets.get(fieldname, "")
	if not linked_doctype:
		return link_value

	try:
		val = frappe.db.get_value(linked_doctype, link_value, subfield)
		if val is None:
			return link_value
		text = str(val)
		import re
		text = re.sub(r"<[^>]+>", " ", text)
		text = re.sub(r"\s+", " ", text).strip()
		return text or link_value
	except Exception:
		return link_value


def _resolve_location(value, fieldname, field_link_targets):
	"""Return a dict with 'text', 'lat', 'lng' for the iCal LOCATION property.

	If the field links to an Address doctype, the record is resolved to a
	comma-separated one-liner plus optional lat/lng from the address_map custom
	fields (latitude, longitude).  For all other field types only 'text' is set.
	"""
	if not value:
		return {"text": "", "lat": None, "lng": None}

	link_target = field_link_targets.get(fieldname, "")

	if link_target == "Address":
		try:
			addr = frappe.db.get_value(
				"Address",
				value,
				["address_line1", "address_line2", "city", "state", "pincode", "country", "latitude", "longitude"],
				as_dict=True,
			)
			if not addr:
				return {"text": value, "lat": None, "lng": None}
			parts = [
				addr.address_line1,
				addr.address_line2,
				addr.city,
				addr.state,
				addr.pincode,
				addr.country,
			]
			text = ", ".join(p.strip() for p in parts if p and str(p).strip())
			lat = float(addr.latitude) if addr.get("latitude") else None
			lng = float(addr.longitude) if addr.get("longitude") else None
			return {"text": text or value, "lat": lat, "lng": lng}
		except Exception:
			return {"text": value, "lat": None, "lng": None}

	# Strip HTML tags for other text/small-text fields so the value is readable
	# in calendar apps (e.g. the address_details HTML field).
	import re
	clean = re.sub(r"<[^>]+>", " ", value)
	clean = re.sub(r"\s+", " ", clean).strip()
	return {"text": clean, "lat": None, "lng": None}
