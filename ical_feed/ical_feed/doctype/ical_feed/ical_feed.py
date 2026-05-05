# Copyright (c) 2026, Beveren Software and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class iCalFeed(Document):
	def before_insert(self):
		self.token = frappe.generate_hash(length=32)

	def validate(self):
		if not self.token:
			self.token = frappe.generate_hash(length=32)
		self._validate_filters()

	def _validate_filters(self):
		if not self.filters_json:
			return
		import json

		try:
			parsed = json.loads(self.filters_json)
			if not isinstance(parsed, list):
				frappe.throw(frappe._("Filters must be a JSON array"))
		except (json.JSONDecodeError, ValueError) as e:
			frappe.throw(frappe._("Filters contain invalid JSON: {0}").format(str(e)))
