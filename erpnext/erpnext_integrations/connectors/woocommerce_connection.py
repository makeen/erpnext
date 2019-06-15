
from __future__ import unicode_literals
import frappe, base64, hashlib, hmac, json
import datetime
from frappe import _
from woocommerce import API


def verify_request():
	woocommerce_settings = frappe.get_doc("Woocommerce Settings")
	sig = base64.b64encode(
		hmac.new(
			woocommerce_settings.secret.encode('utf8'),
			frappe.request.data,
			hashlib.sha256
		).digest()
	)

	if frappe.request.data and \
		frappe.get_request_header("X-Wc-Webhook-Signature") and \
		not sig == bytes(frappe.get_request_header("X-Wc-Webhook-Signature").encode()):
			frappe.throw(_("Unverified Webhook Data"))
	frappe.set_user(woocommerce_settings.creation_user)
	frappe.set_user_lang(woocommerce_settings.creation_user)

@frappe.whitelist(allow_guest=True)
def order():
	woocommerce_settings = frappe.get_doc("Woocommerce Settings")
	if frappe.flags.woocomm_test_order_data:
		fd = frappe.flags.woocomm_test_order_data
		event = "created"

	elif frappe.request and frappe.request.data:
		verify_request()
		fd = json.loads(frappe.request.data)
		event = frappe.get_request_header("X-Wc-Webhook-Event")

	else:
		return "success"

	if event == "created" or event == "updated":
		raw_billing_data = fd.get("billing")
		for meta_data in fd.get("meta_data"):
			if meta_data['key'] == woocommerce_settings.tax_id_fieldname:
				raw_billing_data["tax_id"] = meta_data['value']
		raw_shipping_data = fd.get("shipping")
		customer_woo_com_email = raw_billing_data.get("email")

		if frappe.get_value("Customer",{"woocommerce_email": customer_woo_com_email}):
			# Edit
			link_customer_and_address(raw_billing_data,raw_shipping_data,1)
		else:
			# Create
			link_customer_and_address(raw_billing_data,raw_shipping_data,0)

		default_set_company = frappe.get_doc("Global Defaults")
		company = default_set_company.default_company
		found_company = frappe.get_doc("Company",{"name":company})
		company_abbr = found_company.abbr

		items_list = fd.get("line_items")
		for item in items_list:

			# get tax_class from woocommerce and try to correspond with erpnext tax account by name
			# or use tax account from settings if not
			item_tax_class = item.get("tax_class").upper()
			if not frappe.db.exists("Account", item_tax_class):
				item_tax_class = woocommerce_settings.tax_account[:woocommerce_settings.tax_account.find(" - ")]
			if item_tax_class:
				tax_account = frappe.get_doc("Account",{"account_name":item_tax_class})
				item.update({'tax': {"description": tax_account.account_name, "tax_type": tax_account.account_name + " - " + company_abbr, "tax_rate":tax_account.tax_rate}})
			
			link_item(item,company_abbr)

		customer_name = raw_billing_data.get("first_name") + " " + raw_billing_data.get("last_name")

		woo_order_id = fd.get("id")

		if not frappe.get_value("Sales Order",{"woocommerce_id": woo_order_id}):
			sales_order = frappe.new_doc("Sales Order")
			create = 1
		else:
			#todo: support updated event
			return
			sales_order = frappe.get_doc("Sales Order",{"woocommerce_id": woo_order_id})
			create = 0

		sales_order.customer = customer_name

		created_date = fd.get("date_created").split("T")
		sales_order.transaction_date = created_date[0]

		sales_order.po_no = woo_order_id
		sales_order.woocommerce_id = woo_order_id
		sales_order.naming_series = woocommerce_settings.sales_order_series or "SO-WOO-"

		placed_order_date = created_date[0]
		raw_date = datetime.datetime.strptime(placed_order_date, "%Y-%m-%d")
		raw_delivery_date = frappe.utils.add_to_date(raw_date,days = 7)
		order_delivery_date_str = raw_delivery_date.strftime('%Y-%m-%d')
		order_delivery_date = str(order_delivery_date_str)

		sales_order.delivery_date = order_delivery_date
	
		sales_order.company = company

		item_taxes = []
		for item in items_list:
			woo_product_id = item.get("product_id")
			woo_variation_id = item.get("variation_id")
			woo_id = woo_product_id
			if woo_variation_id:
				woo_id = woo_variation_id
			found_item = frappe.get_doc("Item",{"woocommerce_id": woo_id})

			ordered_items_tax = item.get("total_tax")

			sales_order.append("items",{
				"item_code": found_item.item_code,
				"item_name": found_item.item_name,
				"description": found_item.item_name,
				"delivery_date":order_delivery_date,
				"uom": woocommerce_settings.uom or _("Nos"),
				"qty": item.get("quantity"),
				"rate": item.get("price"),
				"warehouse": woocommerce_settings.warehouse or "Stores" + " - " + company_abbr
			})
			
			item_tax = item.get("tax")
			if item_tax and item_tax.get("tax_type") not in item_taxes:
				item_taxes.append(item_tax.get("tax_type"))
				sales_order.append("taxes",{
					"charge_type":"On Net Total",
					"account_head": item_tax.get("tax_type"),
					"tax_rate": item_tax.get("tax_rate"),
					"description": item_tax.get("description")
				})

		# shipping_details = fd.get("shipping_lines") # used for detailed order
		shipping_total = fd.get("shipping_total")
		shipping_tax = fd.get("shipping_tax")
		sales_order.append("taxes",{
			"charge_type":"Actual",
			"account_head": woocommerce_settings.f_n_f_account,
			"tax_amount": shipping_tax,
			"description": _("Shipping Tax")
		})
		sales_order.append("taxes",{
			"charge_type":"Actual",
			"account_head": woocommerce_settings.f_n_f_account,
			"tax_amount": shipping_total,
			"description": _("Shipping Total")
		})

		sales_order.submit()

		frappe.db.commit()

def link_customer_and_address(raw_billing_data,raw_shipping_data,customer_status):

	if customer_status == 0:
		# create
		customer = frappe.new_doc("Customer")
		billing = frappe.new_doc("Address")
		shipping = frappe.new_doc("Address")

	if customer_status == 1:
		# Edit
		customer_woo_com_email = raw_billing_data.get("email")
		customer = frappe.get_doc("Customer",{"woocommerce_email": customer_woo_com_email})
		old_name = customer.customer_name

	full_name_billing = str(raw_billing_data.get("first_name"))+ " "+str(raw_billing_data.get("last_name"))
	full_name_shipping = str(raw_shipping_data.get("first_name"))+ " "+str(raw_shipping_data.get("last_name"))
	customer.customer_name = full_name_billing
	customer.tax_id = raw_billing_data.get("tax_id")
	customer.woocommerce_email = str(raw_billing_data.get("email"))
	customer.save()
	frappe.db.commit()

	if customer_status == 1:
		frappe.rename_doc("Customer", old_name, full_name_billing)
		billing = frappe.get_doc("Address",{"woocommerce_email":customer_woo_com_email, "address_type": "Billing"})
		shipping = frappe.get_doc("Address",{"woocommerce_email":customer_woo_com_email, "address_type": "Shipping"})
		customer = frappe.get_doc("Customer",{"woocommerce_email": customer_woo_com_email})
	
	billing.address_title = full_name_billing
	billing.address_line1 = raw_billing_data.get("address_1", "Not Provided")
	billing.address_line2 = raw_billing_data.get("address_2", "Not Provided")
	billing.city = raw_billing_data.get("city", "Not Provided")
	billing.woocommerce_email = str(raw_billing_data.get("email"))
	billing.address_type = "Billing"
	billing.country = frappe.get_value("Country", filters={"code":raw_billing_data.get("country", "IN").lower()})
	billing.state =  raw_billing_data.get("state")
	billing.pincode =  str(raw_billing_data.get("postcode"))
	billing.phone = str(raw_billing_data.get("phone"))
	billing.email_id = str(raw_billing_data.get("email"))

	shipping.address_title = full_name_shipping
	shipping.address_line1 = raw_shipping_data.get("address_1", "Not Provided")
	shipping.address_line2 = raw_shipping_data.get("address_2", "Not Provided")
	shipping.city = raw_shipping_data.get("city", "Not Provided")
	if customer_status == 0:
		shipping.woocommerce_email = str(raw_billing_data.get("email"))
	shipping.address_type = "Shipping"
	shipping.country = frappe.get_value("Country", filters={"code":raw_shipping_data.get("country", "IN").lower()})
	shipping.state =  raw_shipping_data.get("state")
	shipping.pincode =  str(raw_shipping_data.get("postcode"))
	if customer_status == 0:
		shipping.phone = str(raw_billing_data.get("phone"))
	shipping.email_id = str(raw_billing_data.get("email"))

	billing.append("links", {
		"link_doctype": "Customer",
		"link_name": customer.customer_name
	})
	shipping.append("links", {
		"link_doctype": "Customer",
		"link_name": customer.customer_name
	})

	billing.save()
	shipping.save()
	frappe.db.commit()

	if customer_status == 1:

		frappe.rename_doc("Address", billing.name, customer.customer_name+"-"+_("Billing"))
		frappe.rename_doc("Address", shipping.name, customer.customer_name+"-"+_("Shipping"))

	frappe.db.commit()

def link_item(item_data,company_abbr):
	woocommerce_settings = frappe.get_doc("Woocommerce Settings")

	woo_product_id = item_data.get("product_id")
	woo_variation_id = item_data.get("variation_id")
	woo_id = woo_product_id
	if woo_variation_id:
		woo_id = woo_variation_id
	
	if not frappe.get_value("Item",{"woocommerce_id": woo_id}):
		#Create Item
		item = frappe.new_doc("Item")
	else:
		#Edit Item
		item = frappe.get_doc("Item",{"woocommerce_id": woo_id})

	#order data come in customer's language, we use woocommerce api for get correct language
	wcapi = API(
		url=woocommerce_settings.woocommerce_server_url,
		consumer_key=woocommerce_settings.api_consumer_key,
		consumer_secret=woocommerce_settings.api_consumer_secret,
		version="wc/v3"
	)
	woo_product = wcapi.get("products/"+str(woo_product_id)+"?lang="+frappe.local.lang,params={"lang":frappe.local.lang}).json()
	if woo_variation_id:
		woo_variation = wcapi.get("products/"+str(woo_product_id)+"/variations/"+str(woo_variation_id)+"?lang="+frappe.local.lang,params={"lang":frappe.local.lang}).json()
		woo_product["name"] += " " + woo_variation["attributes"][0]["option"]
	item.item_name = str(woo_product["name"])
	item.description = str(woo_product["short_description"])+"<br>[[short description]]<br>"+str(woo_product["description"])
	item.item_code = str(item_data.get("sku"))

	if len(item.get("taxes")) == 0 and item_data.get("tax"):
		item.append("taxes",{"tax_type": item_data.get("tax").get("tax_type"), "tax_rate":item_data.get("tax").get("tax_rate")})
	item.woocommerce_id = str(woo_id)
	item.item_group = _("WooCommerce Products")
	item.stock_uom = woocommerce_settings.uom or _("Nos")
	item.save()
	frappe.db.commit()
