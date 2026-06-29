import json
import frappe
from frappe.utils import flt
from erpnext.manufacturing.doctype.job_card.job_card import make_time_log
from erpnext.manufacturing.doctype.work_order.work_order import create_job_card
from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry
from frappe.utils import now_datetime, cint


@frappe.whitelist()
def process_bulk_job_execution(payload):
    if isinstance(payload, str):
        payload = json.loads(payload)

    rows = payload.get("rows") or []
    summary = []

    if not rows:
        return {"summary": []}

    for r in rows:
        result = process_single_row(r)
        summary.append(result)

    return {
        "summary": summary
    }

def process_single_row(r):
    result = {
        "job_card": r.get("job_card"),
        "work_order_id": r.get("work_order_id"),
        "operation": r.get("operation"),
        "requested_qty": flt(r.get("shift_qty")),
        "status": "FAILED",
        "message": "",
        "new_job_card": None,
        "remaining_qty": 0,
        "custom_unique_id": None
    }

    try:
        job_card_name = r.get("job_card")
        employee = r.get("employee")
        qty = flt(r.get("shift_qty"))
        shift = r.get("shift")
        time = r.get("time")
        custom_unique_id = r.get("custom_unique_id")

        if not job_card_name or not employee or qty <= 0:
            result["message"] = "Missing Job Card / Employee / Qty"
            return result

        jc = frappe.get_doc("Job Card", job_card_name)

        if jc.docstatus != 0:
            result["message"] = "Job Card already submitted"
            return result

        original_qty = flt(jc.for_quantity)

        if qty > original_qty:
            result["message"] = "Qty exceeds Job Card balance"
            return result

        # # 🔹 START TIME LOG
        # make_time_log({
        #     "job_card_id": jc.name,
        #     "start_time": now_datetime(),
        #     "employees": [{"employee": employee}],
        #     "status": "Work In Progress",
        #     "shift": r.get("shift")
        # })

        # # 🔹 COMPLETE TIME LOG
        # make_time_log({
        #     "job_card_id": jc.name,
        #     "complete_time": now_datetime(),
        #     "completed_qty": qty,
        #     "status": "Complete",
        # })
        # START LOG
        make_time_log({
            "job_card_id": jc.name,
            "start_time": now_datetime(),
            "employees": [{"employee": employee}],
            "status": "Work In Progress",
        })

        jc.reload()

        # set shift on last time log
        jc.time_logs[-1].custom_current_shift = shift
        jc.time_logs[-1].custom_shift_time = time
        jc.save()


        # COMPLETE LOG
        make_time_log({
            "job_card_id": jc.name,
            "complete_time": now_datetime(),
            "completed_qty": qty,
            "status": "Complete",
        })

        jc.reload()

        # set shift on last time log again
        jc.time_logs[-1].custom_current_shift = shift
        jc.save()
        jc.reload()   # 🔥 THIS IS THE FIX

        jc.custom_unique_id = custom_unique_id
        jc.for_quantity = qty
        jc.process_loss_qty = 0
        jc.flags.ignore_validate_update_after_submit = True
        jc.submit()

        remaining_qty = original_qty - qty
        result["remaining_qty"] = remaining_qty
        result["custom_unique_id"] = custom_unique_id

        # 🔹 CREATE BALANCE JOB CARD
        if remaining_qty > 0:
            new_jc = create_balance_job_card(
                jc=jc,
                remaining_qty=remaining_qty
            )
            result["new_job_card"] = new_jc.name

            new_jc_doc = frappe.get_doc("Job Card", new_jc.name)

            # 🔥 Set unique ID
            if custom_unique_id:
                new_jc_doc.custom_unique_id = custom_unique_id
                new_jc_doc.save(ignore_permissions=True)
            result["message"] = "Partial completed, new Job Card created"
        else:
            result["message"] = "Job Card fully completed"

        result["status"] = "SUCCESS"
        return result

    except Exception as e:
        frappe.log_error(
            frappe.get_traceback(),
            "Bulk Production Operation Error"
        )
        result["message"] = str(e)
        return result

def create_balance_job_card(jc, remaining_qty):

    work_order = frappe.get_doc("Work Order", jc.work_order)

    wo_op_name = frappe.get_value(
        "Work Order Operation",
        {
            "parent": work_order.name,
            "operation": jc.operation
        },
        "name"
    )

    if not wo_op_name:
        raise Exception(
            f"Operation {jc.operation} not found in Work Order {work_order.name}"
        )

    wo_op = frappe.get_doc("Work Order Operation", wo_op_name)
    wo_op.job_card_qty = remaining_qty

    ms = frappe.get_doc("Manufacturing Settings")
    enable_capacity_planning = not cint(ms.disable_capacity_planning)

    return create_job_card(
        work_order=work_order,
        row=wo_op,
        auto_create=True,
        enable_capacity_planning=enable_capacity_planning
    )


@frappe.whitelist()
def bulk_mark_operations_completed(not_selected,selected):

    if isinstance(not_selected, str):
        not_selected = json.loads(not_selected)

    if isinstance(selected, str):
        selected = json.loads(selected)

    frappe.log_error(
        "Bulk Mark Operations Completed",f"Not Selected: {not_selected}, Selected: {selected}"
    )

    default_fg_wh = frappe.db.get_single_value(
        "Bulk Production Settings", "fg_warehouse"
    )

    default_wip_wh = frappe.db.get_single_value(
        "Bulk Production Settings", "wip_warehouse"
    )

    # get latest draft Bulk Production
    bulk_name = frappe.db.get_value(
        "Bulk Production",
        {"docstatus": 0},
        "name",
        order_by="creation desc"
    )


    bulk_doc = None
    if bulk_name:
        bulk_doc = frappe.get_doc("Bulk Production", bulk_name)

    if not_selected:
        updated = False

        for row in bulk_doc.work_order_list:
            if row.work_order_id in not_selected:
                row.operations_completed = 1
                updated = True  

        if updated:
            bulk_doc.save()

    if selected:
            
        try:

            for wo_id in selected:

                wo = frappe.get_doc("Work Order", wo_id)

                manufactured_qty = wo.qty

                if manufactured_qty <= 0:
                    continue

                # ---------------- CREATE STOCK ENTRY ----------------

                se_response = make_stock_entry(
                    work_order_id=wo_id,
                    purpose="Manufacture",
                    qty=manufactured_qty
                )

                if isinstance(se_response, dict) and "message" in se_response:
                    se_response = se_response["message"]

                if isinstance(se_response, dict):
                    se_doc = frappe.get_doc(se_response)
                else:
                    se_doc = se_response

                # ---------------- WAREHOUSE FIX ----------------

                if se_doc.from_warehouse and frappe.db.get_value(
                    "Warehouse", se_doc.from_warehouse, "is_group"
                ):
                    se_doc.from_warehouse = default_wip_wh or default_fg_wh

                if se_doc.to_warehouse and frappe.db.get_value(
                    "Warehouse", se_doc.to_warehouse, "is_group"
                ):
                    se_doc.to_warehouse = default_fg_wh

                for item in se_doc.items:

                    if item.s_warehouse and frappe.db.get_value(
                        "Warehouse", item.s_warehouse, "is_group"
                    ):
                        item.s_warehouse = default_wip_wh or default_fg_wh

                    if item.t_warehouse and frappe.db.get_value(
                        "Warehouse", item.t_warehouse, "is_group"
                    ):
                        item.t_warehouse = default_fg_wh

                    if item.is_finished_item and not item.t_warehouse:
                        item.t_warehouse = default_fg_wh

                se_doc.flags.ignore_permissions = True
                se_doc.insert()
                se_doc.submit()

                # ---------------- UPDATE BULK PRODUCTION ----------------

                if bulk_doc:

                    for row in bulk_doc.work_order_list:

                        if row.work_order_id == wo_id:
                            row.operations_completed = 1
                            # row.manufactured_today = manufactured_qty
                            row.status = "Completed"
                            row.closing_balance = 0
                            row.closing_status = "Completed"
                            row.balance_quantity = 0
                            row.quantity_produced = manufactured_qty

            if bulk_doc:
                bulk_doc.save(ignore_permissions=True)

            frappe.db.commit()

            return {
                "status": "success",
                "message": "Operations marked completed"
            }

        except Exception as e:

            frappe.db.rollback()

            frappe.log_error(
                frappe.get_traceback(),
                "Bulk Manufacture Failed"
            )

            return {
                "status": "error",
                "message": str(e)
            }

# @frappe.whitelist()
# def rollback_job_card(job_card, work_order, operation):


    # jc = frappe.get_doc("Job Card", job_card)

    # if jc.docstatus != 1:
    #     frappe.throw("Only submitted Job Cards can be rolled back")

    # # get quantity produced in this job card
    # produced_qty = jc.for_quantity or 0

    # # find your child table row
    # rows = frappe.get_all(
    #     "Work Order Operations List",
    #     filters={
    #         "work_order_id": work_order,
    #         "operation": operation
    #     },
    #     fields=["name", "parent", "completed_qty", "remaining_qty"]
    # )

    # if rows:

    #     row = rows[0]

    #     completed = row.completed_qty or 0
    #     remaining = row.remaining_qty or 0

    #     new_completed = completed - produced_qty
    #     new_remaining = remaining + produced_qty

    #     frappe.db.set_value(
    #         "Work Order Operations List",
    #         row.name,
    #         {
    #             "completed_qty": new_completed,
    #             "remaining_qty": new_remaining
    #         }
    #     )

    # # cancel job card
    # jc.cancel()

    # frappe.db.commit()

    # return True

@frappe.whitelist()
def rollback_job_card(job_card, work_order, operation):

    jc = frappe.get_doc("Job Card", job_card)

    if jc.docstatus != 1:
        frappe.throw("Only submitted Job Cards can be rolled back")

    produced_qty = jc.for_quantity or 0

    rows = frappe.get_all(
        "Work Order Operations List",
        filters={
            "work_order_id": work_order,
            "operation": operation
        },
        fields=["name", "completed_qty", "remaining_qty"]
    )

    if not rows:
        frappe.throw("Operation row not found")

    row = rows[0]

    completed = row.completed_qty or 0
    remaining = row.remaining_qty or 0

    new_completed = max(completed - produced_qty, 0)
    new_remaining = remaining + produced_qty

    frappe.db.set_value(
        "Work Order Operations List",
        row.name,
        {
            "completed_qty": new_completed,
            "remaining_qty": new_remaining
        }
    )

    # cancel old job card
    jc.cancel()

    # ----------------------------
    # Create replacement Job Card
    # ----------------------------

    new_job_cards = create_balance_job_card(jc, new_remaining)

    # create_job_card() usually returns list
    new_job_card_name = None

    if new_job_cards:
        new_job_card_name = new_job_cards.name

    # store new job card id in child row
    if new_job_card_name:
        frappe.db.set_value(
            "Work Order Operations List",
            row.name,
            "job_card",
            new_job_card_name
        )

    frappe.db.commit()

    return {
        "new_job_card": new_job_card_name
    }