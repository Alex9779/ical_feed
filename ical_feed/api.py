# Copyright (c) 2026, Beveren Software and contributors
# For license information, please see license.txt

import datetime
import json
import re
from html.parser import HTMLParser

import frappe
from frappe import _


# ---------------------------------------------------------------------------
# HTML-to-text parser — defined at module level so the class is not
# re-created on every call to _html_to_text.
# ---------------------------------------------------------------------------

class _HtmlParser(HTMLParser):
	_BLOCK = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr"})

	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.parts: list[str] = []

	def handle_starttag(self, tag, attrs):
		if tag in self._BLOCK:
			self.parts.append("\n")

	def handle_endtag(self, tag):
		if tag in self._BLOCK:
			self.parts.append("\n")

	def handle_data(self, data):
		self.parts.append(data)


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
			"duration",
			"field_summary",
			"field_summary_subfield",
			"field_description",
			"field_description_subfield",
			"field_location",
			"field_location_subfield",
			"address_type_priority",
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
	fetch_fields = list(
		{"name", feed.field_start, feed.field_end}
		| {f for f in (feed.field_summary, feed.field_description, feed.field_location) if f}
	)

	records = frappe.get_list(
		feed.doctype_name,
		filters=filters,
		fields=fetch_fields,
		limit_page_length=5000,
		ignore_permissions=True,  # token is the authorization
	)

	# --- Build ICS -----------------------------------------------------------
	try:
		from icalendar import Calendar, Event, vGeo, vText as _vText
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

	# --- Pre-build subfield caches (batch DB lookups instead of per-record) --
	# One query per unique (parent_field, subfield) pair replaces N individual
	# frappe.db.get_value calls inside the event loop.
	subfield_cache: dict[tuple[str, str], dict[str, str]] = {}
	for _pf, _sf in (
		(feed.field_summary, feed.field_summary_subfield),
		(feed.field_description, feed.field_description_subfield),
		(feed.field_location, feed.field_location_subfield),
	):
		if not _pf or not _sf or (_pf, _sf) in subfield_cache:
			continue
		_linked_dt = field_link_targets.get(_pf, "")
		if not _linked_dt:
			continue
		_link_vals = list({str(r.get(_pf) or "") for r in records if r.get(_pf)})
		if not _link_vals:
			subfield_cache[(_pf, _sf)] = {}
			continue
		try:
			_rows = frappe.get_all(
				_linked_dt,
				filters=[["name", "in", _link_vals]],
				fields=["name", _sf],
				ignore_permissions=True,
			)
			subfield_cache[(_pf, _sf)] = {
				str(row.name): _html_to_text(str(row.get(_sf) or ""))
				for row in _rows
			}
		except Exception:
			subfield_cache[(_pf, _sf)] = {}

	# Parse address type priority once
	_priority_raw = str(feed.address_type_priority or "").strip()
	_address_type_priority = [t.strip() for t in _priority_raw.split(",") if t.strip()] if _priority_raw else []

	# --- Pre-build Address location cache ------------------------------------
	# Batch-fetch all Address records referenced by the location field so the
	# event loop does a dict lookup instead of a DB query per record.
	address_cache: dict[str, dict] = {}
	if feed.field_location and not feed.field_location_subfield:
		if field_link_targets.get(feed.field_location) == "Address":
			_loc_keys = list({str(r.get(feed.field_location) or "") for r in records if r.get(feed.field_location)})
			if _loc_keys:
				try:
					_addr_rows = frappe.get_all(
						"Address",
						filters=[["name", "in", _loc_keys]],
						fields=["name", "address_line1", "address_line2", "city", "state", "pincode", "country", "latitude", "longitude"],
						ignore_permissions=True,
					)
					for _addr in _addr_rows:
						_parts = [_addr.address_line1, _addr.address_line2, _addr.city, _addr.state, _addr.pincode, _addr.country]
						_text = ", ".join(p.strip() for p in _parts if p and str(p).strip())
						address_cache[str(_addr.name)] = {
							"text": _text or str(_addr.name),
							"lat": float(_addr.latitude) if _addr.get("latitude") else None,
							"lng": float(_addr.longitude) if _addr.get("longitude") else None,
						}
				except Exception:
					pass

	# --- Pre-build linked-address cache --------------------------------------
	# When field_location links to a non-Address doctype (e.g. Customer),
	# look up addresses via Dynamic Link and pick the best per link_name.
	linked_address_cache: dict[str, dict] = {}
	if feed.field_location and not feed.field_location_subfield:
		_loc_target = field_link_targets.get(feed.field_location, "")
		if _loc_target and _loc_target != "Address":
			_loc_keys = list({str(r.get(feed.field_location) or "") for r in records if r.get(feed.field_location)})
			if _loc_keys:
				try:
					_linked_rows = frappe.db.sql(
						"""
						SELECT
							dl.link_name,
							addr.name AS address_name,
							addr.address_line1,
							addr.address_line2,
							addr.city,
							addr.state,
							addr.pincode,
							addr.country,
							addr.latitude,
							addr.longitude,
							addr.address_type
						FROM `tabAddress` addr
						JOIN `tabDynamic Link` dl
							ON dl.parent = addr.name
							AND dl.parenttype = 'Address'
							AND dl.link_doctype = %(loc_target)s
							AND dl.link_name IN %(loc_keys)s
						""",
						{"loc_target": _loc_target, "loc_keys": _loc_keys},
						as_dict=True,
					)
					_picked = _pick_best_address(list(_linked_rows), _address_type_priority)
					for _row in _picked:
						_parts = [_row.address_line1, _row.address_line2, _row.city, _row.state, _row.pincode, _row.country]
						_text = ", ".join(p.strip() for p in _parts if p and str(p).strip())
						linked_address_cache[str(_row.link_name)] = {
							"text": _text or str(_row.address_name),
							"lat": float(_row.latitude) if _row.get("latitude") else None,
							"lng": float(_row.longitude) if _row.get("longitude") else None,
						}
				except Exception:
					pass

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
	dt_slug = frappe.scrub(feed.doctype_name).replace("_", "-")

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
			duration_minutes = int(feed.duration or 60) or 60
			end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)

		summary_field = feed.field_summary or "name"
		raw_summary = str(r.get(summary_field) or r.get("name") or "")
		if feed.field_summary_subfield:
			summary = subfield_cache.get((summary_field, feed.field_summary_subfield), {}).get(raw_summary, raw_summary)
		else:
			summary = _html_to_text(raw_summary)

		event = Event()
		event.add("uid", f"{feed.name}-{r.name}@frappe-ical")
		event.add("summary", summary)
		event.add("dtstart", start_dt)
		event.add("dtend", end_dt)
		event.add("url", f"{site_url}/app/{dt_slug}/{r.name}")

		if feed.field_description:
			desc_raw = r.get(feed.field_description)
			if desc_raw:
				desc_str = str(desc_raw)
				if feed.field_description_subfield:
					desc = subfield_cache.get((feed.field_description, feed.field_description_subfield), {}).get(desc_str, desc_str)
				else:
					desc = _html_to_text(desc_str)
				if desc:
					event.add("description", desc)

		if feed.field_location:
			loc = r.get(feed.field_location)
			if loc:
				loc_str = str(loc)
				if feed.field_location_subfield:
					loc_text = subfield_cache.get((feed.field_location, feed.field_location_subfield), {}).get(loc_str, loc_str)
					if loc_text:
						event.add("location", loc_text)
				elif loc_str in address_cache:
					loc_result = address_cache[loc_str]
					if loc_result["text"]:
						event.add("location", loc_result["text"])
						if loc_result["lat"] is not None and loc_result["lng"] is not None:
							lat, lng = loc_result["lat"], loc_result["lng"]
							event.add("geo", vGeo((lat, lng)))
							apple_loc = _vText(f"geo:{lat},{lng}")
							apple_loc.params["X-TITLE"] = loc_result["text"]
							event.add("X-APPLE-STRUCTURED-LOCATION", apple_loc)
				elif loc_str in linked_address_cache:
					_la = linked_address_cache[loc_str]
					if _la["text"]:
						event.add("location", _la["text"])
						if _la["lat"] is not None and _la["lng"] is not None:
							lat, lng = _la["lat"], _la["lng"]
							event.add("geo", vGeo((lat, lng)))
							apple_loc = _vText(f"geo:{lat},{lng}")
							apple_loc.params["X-TITLE"] = _la["text"]
							event.add("X-APPLE-STRUCTURED-LOCATION", apple_loc)
					else:
						# Plain text / non-Address link — just strip HTML
						loc_text = _html_to_text(loc_str)
						if loc_text:
							event.add("location", loc_text)

		cal.add_component(event)

	ics_bytes = cal.to_ical()

	frappe.response["type"] = "download"
	frappe.response["filename"] = f"{frappe.scrub(feed.title or feed.name)}.ics"
	frappe.response["filecontent"] = ics_bytes
	frappe.response["content_type"] = "text/calendar; charset=utf-8"
	frappe.response["display_content_as"] = "inline"


# ---------------------------------------------------------------------------
# Address priority helpers
# ---------------------------------------------------------------------------

_DEFAULT_ADDRESS_TYPE_PRIORITY: tuple[str, ...] = (
	"Billing", "Shipping", "Office", "Personal", "Plant", "Postal",
	"Shop", "Subsidiary", "Warehouse", "Current", "Permanent", "Other",
)


def _pick_best_address(rows: list, priority: list[str] | None) -> list:
	"""Keep only one address per link_name according to the priority list."""
	user_list: list[str] = priority or []
	user_set = set(user_list)
	effective: list[str] = list(user_list) + [t for t in _DEFAULT_ADDRESS_TYPE_PRIORITY if t not in user_set]
	priority_index: dict[str, int] = {t: i for i, t in enumerate(effective)}
	best: dict[str, object] = {}
	for row in rows:
		name = str(row.get("link_name") or "")
		addr_type = str(row.get("address_type") or "")
		rank = priority_index.get(addr_type)
		if rank is None:
			if name not in best:
				best[name] = row
			continue
		current_rank = priority_index.get(str((best[name].get("address_type") or "") if name in best else ""), len(effective)) if name in best else len(effective)
		if rank < current_rank:
			best[name] = row
	seen: set[str] = set()
	result = []
	for row in rows:
		name = str(row.get("link_name") or "")
		if name not in seen and best.get(name) is row:
			seen.add(name)
			result.append(row)
	return result


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

def _html_to_text(value: str) -> str:
	"""Convert an HTML string to plain text suitable for iCal properties.

	Block-level tags are converted to newlines so structure is preserved.
	HTML entities are decoded. Remaining tags are stripped.
	Multiple consecutive blank lines are collapsed to a single blank line.
	"""
	if not value:
		return ""
	try:
		p = _HtmlParser()
		p.feed(value)
		lines = [ln.rstrip() for ln in "".join(p.parts).splitlines()]
		return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
	except Exception:
		clean = re.sub(r"<[^>]+>", " ", value)
		return re.sub(r"\s+", " ", clean).strip()


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
