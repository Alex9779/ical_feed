// Copyright (c) 2026, Beveren Software and contributors
// For license information, please see license.txt

frappe.ui.form.on("iCal Feed", {
	refresh(frm) {
		frm.trigger("update_field_options");

		if (!frm.is_new()) {
			const site_url = window.location.origin;
			const https_url = `${site_url}/api/method/ical_feed.api.get_feed?token=${frm.doc.token}`;
			const webcal_url = https_url.replace(/^https?:\/\//, "webcal://");

			frm.add_custom_button(
				__("Copy HTTPS URL"),
				function () {
					frappe.utils.copy_to_clipboard(https_url);
					frappe.show_alert({ message: __("HTTPS URL copied to clipboard!"), indicator: "green" });
				},
				__("Subscribe")
			);

			frm.add_custom_button(
				__("Copy webcal:// URL"),
				function () {
					frappe.utils.copy_to_clipboard(webcal_url);
					frappe.show_alert({ message: __("webcal:// URL copied to clipboard!"), indicator: "green" });
				},
				__("Subscribe")
			);

			frm.add_custom_button(
				__("Subscribe in Calendar App"),
				function () {
					window.open(webcal_url, "_blank");
				},
				__("Subscribe")
			);

			frm.add_custom_button(
				__("Regenerate Token"),
				function () {
					frappe.confirm(
						__("Regenerating will invalidate the existing subscription URL. Continue?"),
						function () {
							frappe.call({
								method: "ical_feed.api.regenerate_token",
								args: { feed_name: frm.doc.name },
								callback(r) {
									if (r.message) {
										frm.reload_doc();
										frappe.show_alert({ message: __("Token regenerated"), indicator: "green" });
									}
								},
							});
						}
					);
				},
				__("Actions")
			);

			frm.add_custom_button(
				__("Preview Record Count"),
				function () {
					let filters = [];
					try {
						filters = JSON.parse(frm.doc.filters_json || "[]");
					} catch (e) {
						frappe.msgprint(__("Filters contain invalid JSON"));
						return;
					}
					frappe.call({
						method: "frappe.client.get_count",
						args: { doctype: frm.doc.doctype_name, filters: filters },
						callback(r) {
							frappe.msgprint(
								__("Feed would include {0} record(s)", [r.message || 0])
							);
						},
					});
				},
				__("Filters")
			);

			frm.add_custom_button(
				__("Edit Filters"),
				function () {
					_open_filter_dialog(frm);
				},
				__("Filters")
			);
		}
	},

	doctype_name(frm) {
		// Clear field selections when doctype changes
		["field_start", "field_end", "field_summary", "field_summary_subfield",
		 "field_description", "field_description_subfield",
		 "field_location", "field_location_subfield"].forEach(
			(f) => frm.set_value(f, "")
		);
		frm.trigger("update_field_options");
	},

	field_summary(frm) { frm.trigger("_update_subfield_for_summary"); },
	field_description(frm) { frm.trigger("_update_subfield_for_description"); },
	field_location(frm) { frm.trigger("_update_subfield_for_location"); },

	_update_subfield_for_summary(frm) {
		_update_link_subfield(frm, "field_summary", "field_summary_subfield");
	},
	_update_subfield_for_description(frm) {
		_update_link_subfield(frm, "field_description", "field_description_subfield");
	},
	_update_subfield_for_location(frm) {
		_update_link_subfield(frm, "field_location", "field_location_subfield");
	},

	update_field_options(frm) {
		const dynamic_fields = [
			"field_start",
			"field_end",
			"field_summary",
			"field_description",
			"field_location",
		];

		if (!frm.doc.doctype_name) {
			dynamic_fields.forEach((f) => frm.set_df_property(f, "options", ""));
			return;
		}

		frappe.model.with_doctype(frm.doc.doctype_name, function () {
			const meta = frappe.get_meta(frm.doc.doctype_name);
			if (!meta) return;

			const datetime_options =
				"\n" +
				meta.fields
					.filter((f) => ["Datetime", "Date"].includes(f.fieldtype))
					.map((f) => f.fieldname)
					.join("\n");

			const all_options =
				"\n" +
				meta.fields.map((f) => f.fieldname).join("\n");

			frm.set_df_property("field_start", "options", datetime_options);
			frm.set_df_property("field_end", "options", datetime_options);
			frm.set_df_property("field_summary", "options", all_options);
			frm.set_df_property("field_description", "options", all_options);
			frm.set_df_property("field_location", "options", all_options);

			frm.refresh_fields(dynamic_fields);

			// Restore subfield selectors for any already-saved Link fields
			["summary", "description", "location"].forEach((key) => {
				_update_link_subfield(frm, `field_${key}`, `field_${key}_subfield`);
			});
		});
	},
});

function _update_link_subfield(frm, parent_field, subfield_field) {
	const fieldname = frm.doc[parent_field];
	if (!fieldname || !frm.doc.doctype_name) {
		frm.set_value(subfield_field, "");
		frm.set_df_property(subfield_field, "hidden", 1);
		frm.refresh_field(subfield_field);
		return;
	}

	frappe.model.with_doctype(frm.doc.doctype_name, function () {
		const meta = frappe.get_meta(frm.doc.doctype_name);
		if (!meta) return;

		const field_meta = meta.fields.find((f) => f.fieldname === fieldname);
		if (!field_meta || field_meta.fieldtype !== "Link" || !field_meta.options) {
			frm.set_value(subfield_field, "");
			frm.set_df_property(subfield_field, "hidden", 1);
			frm.refresh_field(subfield_field);
			return;
		}

		const linked_doctype = field_meta.options;
		frappe.model.with_doctype(linked_doctype, function () {
			const linked_meta = frappe.get_meta(linked_doctype);
			if (!linked_meta) return;

			const SKIP = ["Section Break", "Column Break", "HTML", "Table", "Tab Break", "Button", "Fold"];
			const options =
				"\n" +
				linked_meta.fields
					.filter((f) => !SKIP.includes(f.fieldtype))
					.map((f) => f.fieldname)
					.join("\n");

			frm.set_df_property(subfield_field, "options", options);
			frm.set_df_property(subfield_field, "hidden", 0);
			frm.set_df_property(subfield_field, "description",
				__("From linked {0} record", [linked_doctype]));
			frm.refresh_field(subfield_field);
		});
	});
}

function _open_filter_dialog(frm) {
	if (!frm.doc.doctype_name) {
		frappe.msgprint(__("Please select a DocType first"));
		return;
	}

	const dialog = new frappe.ui.Dialog({
		title: __("Edit Filters for {0}", [frm.doc.doctype_name]),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "filter_area",
				options: "<div class='ical-filter-area'></div>",
			},
		],
		primary_action_label: __("Apply"),
		primary_action() {
			const filters = filter_group.get_filters();
			frm.set_value(
				"filters_json",
				filters.length ? JSON.stringify(filters, null, 2) : ""
			);
			dialog.hide();
			frm.save();
		},
	});

	dialog.show();

	const $parent = dialog.fields_dict.filter_area.$wrapper.find(".ical-filter-area");

	const filter_group = new frappe.ui.FilterGroup({
		parent: $parent,
		doctype: frm.doc.doctype_name,
		on_change: () => {},
	});

	let existing = [];
	try {
		existing = JSON.parse(frm.doc.filters_json || "[]");
	} catch (_) {}

	if (existing.length) {
		filter_group.add_filters_to_filter_group(existing);
	}
}
